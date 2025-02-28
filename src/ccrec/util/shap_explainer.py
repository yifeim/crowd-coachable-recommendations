import matplotlib.pyplot as plt
from matplotlib import transforms
import dataclasses, typing, torch
import shap
from shap.plots._text import unpack_shap_explanation_contents, process_shap_values, colors


def values_min_max(values, base_values):
    """ Used to pick our axis limits.
    """
    fx = base_values + values.sum()
    xmin = fx - values[values > 0].sum()
    xmax = fx - values[values < 0].sum()
    cmax = max(abs(values.min()), abs(values.max()))
    d = xmax - xmin
    xmin -= 0.1 * d
    xmax += 0.1 * d

    return xmin, xmax, cmax


def get_tokens_and_colors(shap_values, num_starting_labels=0, grouping_threshold=0.01, separator=''):
    # set any unset bounds
    xmin, xmax, cmax = values_min_max(shap_values.values, shap_values.base_values)

    values, clustering = unpack_shap_explanation_contents(shap_values)
    tokens, values, group_sizes = process_shap_values(
        shap_values.data, values, grouping_threshold, separator, clustering)

    return tokens, 0.5 + 0.5 * values / (cmax + 1e-8)


def rainbow_text(x, y, ls, lc, width=40, nrows=4, **kw):
    """ https://stackoverflow.com/a/9185851
    https://stackoverflow.com/q/23696898 """

    t = plt.gca().transData
    fig = plt.gcf()
    # plt.show()
    cur_words = 0
    cur_x = 0
    cur_rows = 0

    for i, (s, c) in enumerate(zip(ls, lc)):
        text = plt.text(x, y, s, color=c if isinstance(c, str) else 'black',
                        transform=t, **kw)
        if not isinstance(c, str):
            color = colors.red_transparent_blue(c)
            text.set_bbox(dict(facecolor=color, edgecolor='none', pad=0, boxstyle='round'))
        text.draw(fig.canvas.get_renderer())
        ex = text.get_window_extent()
        if cur_words + len(s) >= width:
            t = transforms.offset_copy(text._transform, x=-cur_x, y=-ex.height * 1.2, units='dots')
            cur_words = 0
            cur_x = 0
            cur_rows += 1
        else:
            t = transforms.offset_copy(text._transform, x=ex.width, units='dots')
            cur_words += len(s)
            cur_x += ex.width
        if cur_rows >= nrows:
            break


def plot_shap_values(x, y, shap_values, **kw):
    assert not hasattr(shap_values, '__iter__'), "single instance at a time"
    ls, lc = get_tokens_and_colors(shap_values)
    rainbow_text(x, y, ls, lc, **kw)


@dataclasses.dataclass
class I2IExplainer:
    item_tower: typing.Callable  # cuda, eval
    tokenizer: typing.Any
    fixed_context: int = 0  # 0 yields sparser results
    max_length: int = 128

    @property
    def tokenizer_kw(self):
        return dict(padding=True, return_tensors='pt', max_length=self.max_length, truncation=True)

    @torch.no_grad()
    def __call__(self, given, cand_texts):
        _inputs = self.tokenizer(given, **self.tokenizer_kw)
        x = self.item_tower(**_inputs)
        x = x.mean(0, keepdims=True)

        @torch.no_grad()
        def f(cand_texts):
            _inputs = self.tokenizer(cand_texts.tolist(), **self.tokenizer_kw)
            y = self.item_tower(**_inputs)
            return (x * y).sum(-1).cpu().numpy()

        explainer = shap.Explainer(f, self.tokenizer)
        return explainer(cand_texts, fixed_context=self.fixed_context)
