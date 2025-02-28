import os, warnings, dataclasses, collections, itertools, time, functools, typing
import pandas as pd, numpy as np, scipy.sparse as sps
from pytorch_lightning.loggers import TensorBoardLogger
from ccrec.util import merge_unique
from rime.dataset import Dataset
from rime.util import indices2csr, perplexity, matrix_reindex


def create_zero_shot(item_df, self_training=False):
    user_df = pd.DataFrame({"TEST_START_TIME": [1] * len(item_df)})  # naturally indexed
    event_df = pd.DataFrame({
        'USER_ID': np.arange(len(item_df)),
        'ITEM_ID': item_df.index.values,
        'TIMESTAMP': 0,
        'VALUE': 1,
    })
    if self_training:
        event_df = pd.concat([event_df, event_df.assign(TIMESTAMP=1)], ignore_index=True)
    return Dataset(user_df, item_df, event_df)


def _sanitize_inputs(event_df, user_df, item_df, clear_future_events=None):
    assert user_df.index.is_unique, "require unique user ids"
    if event_df is None:
        user_non_empty = user_df[user_df['_hist_len'] > 0]
        event_df = user_non_empty['_hist_items'].explode().to_frame("ITEM_ID").assign(
            TIMESTAMP=user_non_empty['_hist_ts'].explode().values,
            VALUE=user_non_empty['_hist_values'].explode().values,
            USER_ID=lambda x: x.index.get_level_values(0),
        )[['USER_ID', 'ITEM_ID', 'TIMESTAMP', 'VALUE']].reset_index(drop=True)

    assert event_df['TIMESTAMP'].max() < time.time(), "require TIMESTAMP < current request_time"
    if 'VALUE' not in event_df:
        event_df = event_df.assign(VALUE=1)

    event_old, event_df = event_df, event_df[event_df['USER_ID'].isin(user_df.index) &
                                             event_df['ITEM_ID'].isin(item_df.index)].copy()
    if len(event_old) > len(event_df):
        print(f"filtering events by known USER_ID and ITEM_ID. #events {len(event_old)} -> {len(event_df)}")

    past_event_df = event_df.join(
        user_df.groupby(level=0).first()[['TEST_START_TIME']], on='USER_ID'
    ).query("TIMESTAMP < TEST_START_TIME").drop("TEST_START_TIME", axis=1)

    if len(past_event_df) < len(event_df):
        if clear_future_events is None:
            warnings.warn(f'future event detected, rate={1 - len(past_event_df) / len(event_df):.1%}')
        elif clear_future_events:
            print(f"removing future events, rate={1 - len(past_event_df) / len(event_df):.1%}")
            event_df = past_event_df

    return event_df


@dataclasses.dataclass
class Env:
    user_df: pd.DataFrame
    item_df: pd.DataFrame
    event_df: pd.DataFrame = None
    prefix: str = 'ccrec-env-'
    sample_size: int = 2
    recording: bool = True
    test_requests: pd.DataFrame = None  # allow multiple requests per user when recording is off
    item_in_test: pd.DataFrame = None
    horizon: float = float("inf")
    clear_future_events: bool = None
    exclude_train: typing.Union[bool, list] = True
    sample_with_prior: float = 0  # negative value = discourage repeats; positive value = reranking tests
    _is_synthetic: bool = True
    _sort_candidates: bool = None  # set default according to _is_synthetic
    _text_width: int = None      # set default according to _is_synthetic
    _text_ellipsis: bool = None  # set default according to _is_synthetic
    _start_step_idx: int = 0

    def __post_init__(self):
        self.name = "{}-{}-{}".format(self.prefix, len(self.user_df), len(self.item_df))
        self._logger = TensorBoardLogger('logs', self.name)
        self._logger.log_hyperparams({k: v for k, v in locals().items() if k in [
            "sample_size", "recording", "horizon", "clear_future_events", "sample_with_prior"]})
        print(f'{self.__class__.__name__} logs at {self._logger.log_dir}')

        self.event_df = _sanitize_inputs(self.event_df, self.user_df, self.item_df, self.clear_future_events)

        if self.test_requests is None:
            self.test_requests = self.user_df.set_index("TEST_START_TIME", append=True)

        if self.item_in_test is None:
            self.item_in_test = self.item_df[self.item_df.index != 'Other']

        if self.recording:
            assert self.test_requests.index.get_level_values(0).is_unique, \
                "expect unique USER_ID in test_requests when recording is on"
            assert self.horizon == float("inf"), "expect horizon=inf when recording is on"

        if self._sort_candidates is None:
            self._sort_candidates = self._is_synthetic
        if self._text_width is None:
            self._text_width = 10 if self._is_synthetic else 160
        if self._text_ellipsis is None:
            self._text_ellipsis = not self._is_synthetic

        self._tokenize = {k: j for j, k in enumerate(self.item_df.index)}
        self._response = {}
        self._reward_by_policy = []

    @functools.cached_property
    def _item_titles(self):
        item_df = self.item_df if 'TITLE' in self.item_df else \
                  self.item_df.assign(TITLE=self.item_df.index.astype(str))
        return item_df['TITLE'].apply(
            lambda x: x if len(x) < self._text_width else
            x[:self._text_width - 4 * self._text_ellipsis] + ' ...' * self._text_ellipsis)

    def _get_step_idx(self):
        return self._start_step_idx if len(self._response) == 0 else max(self._response.keys()) + 1

    def _last_step_idx(self):
        return self._get_step_idx() - 1

    def step(self, *policies):
        """ response ('USER_ID', 'TEST_START_TIME'), ['_hist_items', 'cand_items', '_group', 'multi_label']) """
        step_idx = self._get_step_idx()
        request, D = self._create_request(*policies)
        self._logger.log_metrics({'request_ppl': _get_request_perplexity(request)}, step_idx)

        response = self._invoke(request, D, step_idx)
        self._response[step_idx] = response

        if self.recording:
            self._update_events(response, step_idx)
        self._logger.log_metrics({'collected_len': len(response),
                                  'collected_sum': np.vstack(response['multi_label']).sum()}, step_idx)

        reward_by_policy = _evaluate_response(response)
        self._reward_by_policy.append(reward_by_policy)
        self._logger.experiment.add_scalars('reward_by_policy', reward_by_policy, step_idx)
        return reward_by_policy

    def _create_request(self, *policies):
        if isinstance(self.test_requests, collections.abc.Callable):
            test_requests = self.test_requests(self.user_df, self.event_df)  # query_least_certain_users
        else:
            test_requests = self.test_requests
        D = self._create_testing_dataset(test_requests=test_requests)

        sample_size = self.sample_size if np.size(self.sample_size) > 1 else [self.sample_size] * len(policies)
        total_size = sum(sample_size)
        J = [p(D, total_size) for p in policies]

        rows = zip(*J)
        display = [merge_unique(lol, sample_size, total_size) for lol in rows]
        display_J, display_groups = zip(*display)

        req = D.test_requests[['_hist_items']].assign(
            cand_items=self.item_in_test.index.values[np.asarray(display_J)].tolist(),
            _group=np.asarray(display_groups).tolist(),
            request_time=time.time())
        req['last_title'] = req['_hist_items'].apply(lambda x: self._item_titles.loc[x[-1]] if len(x) else '(empty)')
        req['cand_titles'] = req['cand_items'].apply(lambda x: self._item_titles.loc[x].tolist())
        req = _sort_or_shuffle(req, self._sort_candidates)
        return req, D  # for SimuEnv

    def _invoke(self, request, D, step_idx):
        return NotImplementedError("return request + multi_label column")

    def _update_events(self, response, step_idx):
        new_events = parse_response(response, step_idx)
        self.event_df = pd.concat([self.event_df, new_events], ignore_index=True)

    def _create_testing_dataset(self, test_requests=None):
        return Dataset(self.user_df, self.item_df, self.event_df,
                       test_requests, self.item_in_test,
                       exclude_train=self.exclude_train,
                       horizon=self.horizon,
                       sample_with_prior=self.sample_with_prior)

    def _create_training_dataset(self, before_step_idx=float('inf'), test_update_history=False):
        test_requests = self.event_df.query(f"0 <= step_idx < {before_step_idx}").groupby(
            ['USER_ID', 'TIMESTAMP']).size().to_frame('_siz')
        return Dataset(self.user_df, self.item_df, self.event_df,
                       test_requests, self.item_in_test,
                       exclude_train=self.exclude_train,
                       horizon=0.01,
                       sample_with_prior=1.0,
                       test_update_history=test_update_history)


def query_least_certain_users(batch_size):
    def fn(user_df, event_df):
        user_context = event_df.join(user_df, on='USER_ID').query('TIMESTAMP < TEST_START_TIME') \
                    .groupby('USER_ID')['ITEM_ID'].first()

        least_certain_users = event_df.join(user_context.to_frame('given'), on='USER_ID').query('given != ITEM_ID') \
                                      .groupby('USER_ID')['VALUE'].sum().reindex(user_df.index, fill_value=0) \
                                      .sort_values().iloc[:batch_size]

        return least_certain_users.to_frame('_value_sum').join(user_df).set_index('TEST_START_TIME', append=True)
    return fn


def _sort_or_shuffle(request, _sort_candidates):
    request = request.assign(
        _reverse_index=request['_group'].apply(
            lambda x: np.argsort(np.asarray(x)[np.asarray(x) != -1], kind='stable')
            if _sort_candidates else np.random.permutation(len(x))
        ))
    return request.assign(
        _group=lambda df: df.apply(
            lambda x: np.asarray(x['_group'])[x['_reverse_index']].tolist(), axis=1),
        cand_items=lambda df: df.apply(
            lambda x: np.asarray(x['cand_items'])[x['_reverse_index']].tolist(), axis=1),
        cand_titles=lambda df: df.apply(
            lambda x: np.asarray(x['cand_titles'])[x['_reverse_index']].tolist(), axis=1),
    ).drop('_reverse_index', axis=1)


def _get_request_perplexity(request):
    ind, cnt = np.unique(np.hstack(request['cand_items']), return_counts=True)
    return perplexity(cnt)


def _expand_na_class(request):
    _expand_cand_items = lambda x: list(x['cand_items']) + [x['_hist_items'][-1]]
    return request.assign(cand_items=request.apply(_expand_cand_items, axis=1),
                           _group=request['_group'].apply(lambda x: x + [-1]))


def parse_response(response, step_idx=None, infer_time_unit=True):
    if step_idx is None:
        step_idx = response['request_time'].rank(method='dense').values - 1
    response = response.assign(step_idx=step_idx).set_index("step_idx", append=True)

    new_events = response['cand_items'].explode().to_frame("ITEM_ID")
    new_events['USER_ID'] = new_events.index.get_level_values(0)
    new_events['TIMESTAMP'] = response['request_time']  # reindex to explode
    new_events['VALUE'] = response['multi_label'].explode().values
    new_events['_group'] = response['_group'].explode().values
    new_events['step_idx'] = new_events.index.get_level_values(-1)

    if infer_time_unit and new_events['TIMESTAMP'].max() > time.time():
        new_events['TIMESTAMP'] = new_events['TIMESTAMP'] / 1e3
    return new_events.reset_index(drop=True)


def _evaluate_response(response):
    data = np.vstack(response['multi_label'])
    group = np.vstack(response['_group'])

    out = {}
    for group_id in np.unique(group):
        pos = data[group == group_id].sum()
        imp = (group == group_id).sum()
        out[str(group_id)] = pos / imp
    return out
