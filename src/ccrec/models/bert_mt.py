import numpy as np, torch, torch.nn.functional as F, tqdm, os, pandas as pd
from pytorch_lightning.trainer.supporters import CombinedLoader
from ccrec.models.bbpr import (
    _Tower, _BertBPR, sps_to_torch, _device_mode_context, auto_device, BertBPR,
    AutoTokenizer, TensorBoardLogger, empty_cache_on_exit, _DataModule, Trainer,
    LightningDataModule, DataLoader, auto_cast_lazy_score, I2IExplainer,
    default_random_split, _LitValidated)
from ccrec.models.vae_models import MaskedPretrainedModel, VAEPretrainedModel
from transformers import DefaultDataCollator, DataCollatorForLanguageModeling
from ccrec.models.vae_lightning import VAEData
import rime
from ccrec.env import create_zero_shot, parse_response


class _TowerMT(_Tower):
    def __init__(self, model, layer_norm='inferred from model'):
        super().__init__(model, torch.nn.Sequential(
            model.vocab_transform,
            model.activation,
            model.standard_layer_norm))

    def forward(self, cls=None, input_step='inputs', output_step='final', **inputs):
        if input_step == 'inputs':
            inputs = {k: v.to(self.model.device) for k, v in inputs.items()}
            if output_step == 'cls':
                mean, std = self.model(**inputs, return_mean_std=True)
                assert std == 0, "calling cls on vae model is ambiguous"
                return mean
            if output_step == 'final':
                return self.model(**inputs, return_embedding=True)  # standard_layer_norm
            elif output_step == 'dict':
                return self.model(**inputs, return_dict=True)  # ct loss and logits

        elif input_step == 'cls':
            if output_step == 'final':
                return self.layer_norm(cls)


class _BertMT(_BertBPR):
    def __init__(self, all_inputs, model_name='distilbert-base-uncased',
                 alpha=0.05, beta=2e-3,
                 n_negatives=10, valid_n_negatives=None, lr=2e-5, weight_decay=0.01,
                 training_prior_fcn=lambda x: x,
                 replacement=True,
                 sample_with_prior=True, sample_with_posterior=0.5,
                 pretrained_checkpoint=None,
                 ):
        super(_BertBPR, self).__init__()
        if valid_n_negatives is None:
            valid_n_negatives = n_negatives
        self.sample_with_prior = sample_with_prior
        self.sample_with_posterior = sample_with_posterior

        self.save_hyperparameters('alpha', 'beta', "n_negatives", "valid_n_negatives",
                                  "lr", "weight_decay", "replacement")
        for name in self.hparams:
            setattr(self, name, getattr(self.hparams, name))
        self.training_prior_fcn = training_prior_fcn

        if pretrained_checkpoint is None:
            pretrained_checkpoint = model_name
        vae_model = VAEPretrainedModel.from_pretrained(pretrained_checkpoint)
        if hasattr(vae_model, 'set_beta'):
            vae_model.set_beta(beta)
        self.item_tower = _TowerMT(vae_model)

        self.all_inputs = all_inputs
        self.alpha = alpha

    def set_training_data(self, ct_cycles=None, ft_cycles=None, **kw):
        super().set_training_data(**kw)
        self.ct_cycles = ct_cycles
        self.ft_cycles = ft_cycles

    def training_and_validation_step(self, batch, batch_idx):
        ijw, inputs = batch
        ft_loss = super().training_and_validation_step(ijw, batch_idx)
        ct_loss = self.item_tower(**inputs, output_step='dict')[0]
        return (1 - self.alpha) / self.ct_cycles * ct_loss.mean() + \
               self.alpha / self.ft_cycles * ft_loss.mean()

    def configure_optimizers(self):
        return torch.optim.AdamW(
            self.parameters(), lr=self.lr, weight_decay=self.weight_decay)


class _DataMT(_DataModule):
    def __init__(self, rime_dataset, item_df, tokenizer, all_inputs, do_validation=None,
                 batch_size=None, valid_batch_size=None, predict_batch_size=64 * torch.cuda.device_count(),
                 **tokenizer_kw):
        super().__init__(rime_dataset, item_df.index, all_inputs, do_validation,
                         batch_size, valid_batch_size, predict_batch_size)
        self._ct = VAEData(item_df, tokenizer, predict_batch_size, do_validation, **tokenizer_kw)
        self.training_data.update({
            'ct_cycles': max(1, self._num_batches / self._ct._num_batches),
            'ft_cycles': max(1, self._ct._num_batches / self._num_batches),
        })
        print('ct_num_batches', self._ct._num_batches, 'ft_num_batches', self._num_batches,
              'ct_cycles', self.training_data['ct_cycles'], 'ft_cycles', self.training_data['ft_cycles'])

    def setup(self, stage):
        super().setup(stage)
        self._ct.setup(stage)

    def train_dataloader(self):
        return CombinedLoader([super().train_dataloader(), self._ct.train_dataloader()],
                               mode='max_size_cycle')

    def val_dataloader(self):
        if self._do_validation:
            return CombinedLoader([super().val_dataloader(), self._ct.val_dataloader()],
                                   mode='max_size_cycle')


class BertMT(BertBPR):
    def __init__(self, item_df, batch_size=10,
                 model_name='distilbert-base-uncased', max_length=30,
                 max_epochs=10, max_steps=-1, do_validation=None,
                 strategy='dp', query_item_position_in_user_history=0,
                 **_model_kw):
        if do_validation is None:
            do_validation = max_epochs > 1

        self.item_titles = item_df['TITLE']
        self.max_length = max_length
        self.batch_size = batch_size
        self.do_validation = do_validation
        self.max_epochs = max_epochs
        self.max_steps = max_steps
        self.strategy = strategy
        self._model_kw = {**_model_kw, 'model_name': model_name}

        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.tokenizer_kw = dict(padding='max_length', max_length=self.max_length, truncation=True)
        self.all_inputs = self.tokenizer(self.item_titles.tolist(), return_tensors='pt', **self.tokenizer_kw)

        self.model = _BertMT(self.all_inputs, **self._model_kw)
        self.valid_batch_size = self.batch_size * self.model.n_negatives * 2 // self.model.valid_n_negatives
        self.predict_batch_size = 64 * torch.cuda.device_count()

        self._ckpt_dirpath = []
        self._logger = TensorBoardLogger('logs', "BertMT")
        self._logger.log_hyperparams({k: v for k, v in locals().items() if k in [
            'batch_size', 'max_epochs', 'max_steps', 'sample_with_prior', 'sample_with_posterior'
        ]})
        print(f'BertMT logs at {self._logger.log_dir}')

    def _get_data_module(self, V):
        return _DataMT(V, self.item_titles.to_frame(), self.tokenizer, self.all_inputs, self.do_validation,
                       self.batch_size, self.valid_batch_size, self.predict_batch_size)

    @empty_cache_on_exit
    def fit(self, V=None):
        if V is None or not any([param.requires_grad for param in self.model.parameters()]):
            return self
        model = _BertMT(self.all_inputs, **self._model_kw)

        dm = self._get_data_module(V)
        model.set_training_data(**dm.training_data)
        max_epochs = int(max(5, self.max_epochs / dm.training_data['ct_cycles']))
        trainer = Trainer(
            max_epochs=max_epochs, max_steps=self.max_steps,
            gpus=torch.cuda.device_count(), strategy=self.strategy,
            log_every_n_steps=1, callbacks=[model._checkpoint])

        trainer.fit(model, datamodule=dm)
        model._load_best_checkpoint("best")

        if not os.path.exists(model._checkpoint.dirpath):  # add manual checkpoint
            print('model.load_state_dict(torch.load(...), strict=False)')
            print(f'{model._checkpoint.dirpath}/state-dict.pth')
            os.makedirs(model._checkpoint.dirpath)
            torch.save(model.state_dict(), model._checkpoint.dirpath + '/state-dict.pth')

        self._logger.experiment.add_text("ckpt", model._checkpoint.dirpath, len(self._ckpt_dirpath))
        self._ckpt_dirpath.append(model._checkpoint.dirpath)
        self.model = model
        return self


def bmt_main(item_df, expl_response, gnd_response, max_epochs=50, alpha=0.05, beta=0.0):
    """
    item_df = get_item_df()[0]
    expl_response = pd.read_json(
        'vae-1000-queries-10-steps-response.json', lines=True, convert_dates=False
    ).set_index('level_0')
    gnd_response = pd.read_json(
        'prime-pantry-i2i-online-baseline4-response.json', lines=True, convert_dates=False
    ).set_index('level_0')
    """
    zero_shot = create_zero_shot(item_df)
    train_requests = expl_response.set_index('request_time', append=True)
    expl_events = parse_response(expl_response)
    V = rime.dataset.Dataset(
        zero_shot.user_df, item_df, pd.concat([zero_shot.event_df, expl_events]),
        test_requests=train_requests[[]], test_update_history=False, horizon=0.1, sample_with_prior=1)

    bmt = BertMT(
        item_df, alpha=alpha, beta=beta,
        max_epochs=50, batch_size=10 * torch.cuda.device_count(),
        sample_with_prior=True, sample_with_posterior=0,
        replacement=False, n_negatives=5, valid_n_negatives=5,
        training_prior_fcn=lambda x: (x + 1 / x.shape[1]).clip(0, None).log(),
    )
    bmt.fit(V)

    gnd_events = parse_response(gnd_response)
    gnd = rime.dataset.Dataset(
        zero_shot.user_df, item_df, pd.concat([zero_shot.event_df, gnd_events]),
        sample_with_prior=1e5)
    gnd._k1 = 1
    reranking_task = rime.Experiment(gnd)
    reranking_task.run({'bmt': bmt})
    recall = reranking_task.item_rec['bmt']['recall']

    return recall, bmt
