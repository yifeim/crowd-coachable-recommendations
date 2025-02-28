from transformers import AutoTokenizer, AutoModel, DefaultDataCollator
from datasets import Dataset
from rime.util import (default_random_split, empty_cache_on_exit, _LitValidated, _ReduceLRLoadCkpt,
                       auto_cast_lazy_score, sps_to_torch, auto_device, timed)
import torch.nn.functional as F
from torch.utils.data import DataLoader
from pytorch_lightning import Trainer, LightningDataModule
from pytorch_lightning.callbacks import LearningRateMonitor
from pytorch_lightning.loggers import TensorBoardLogger
import functools, torch, numpy as np, pandas as pd
import os, itertools, dataclasses, warnings, collections, re, tqdm
from ccrec.util import _device_mode_context
from ccrec.util.shap_explainer import I2IExplainer

# https://pytorch-lightning.readthedocs.io/en/stable/notebooks/lightning_examples/text-transformers.html


def _create_bert(model_name, freeze_bert):
    model = AutoModel.from_pretrained(model_name)
    if freeze_bert > 0:
        for param in model.parameters():
            param.requires_grad = False

    elif freeze_bert < 0:
        for param in model.embeddings.parameters():
            param.requires_grad = False

        for i in range(len(model.encoder.layer) + freeze_bert):
            for param in model.encoder.layer[i].parameters():
                param.requires_grad = False

    return model


class _Tower(torch.nn.Module):
    """ inputs -> model -> cls -> layer_norm -> final """
    def __init__(self, model, layer_norm):
        super().__init__()
        self.model = model
        self.layer_norm = layer_norm

    @property
    def device(self):
        return self.model.device

    def forward(self, cls=None, input_step='inputs', output_step='final', **inputs):
        if input_step == 'inputs':
            inputs = {k: v.to(self.model.device) for k, v in inputs.items()}
            cls = self.model(**inputs).last_hidden_state[:, 0]
        else:  # cls
            cls = cls.to(self.model.device)

        if output_step == 'final':
            return self.layer_norm(cls)
        else:  # cls
            return cls


class _BertBPR(_LitValidated):
    def __init__(self, all_inputs, model_name='bert-base-uncased', freeze_bert=0,
                 n_negatives=10, valid_n_negatives=None, lr=None, weight_decay=None,
                 training_prior_fcn=lambda x: x,
                 do_validation=True, replacement=True,
                 sample_with_prior=True, sample_with_posterior=0.5,
                 elementwise_affine=True,  # set to False to eliminate gradients from f(x)'f(x) in N/A class
                 pretrained_checkpoint=None,
                 **bpr_kw):
        super().__init__()
        if lr is None:
            lr = 0.1 if freeze_bert > 0 else 1e-4
        if weight_decay is None:
            weight_decay = 1e-5 if freeze_bert > 0 else 0
        if valid_n_negatives is None:
            valid_n_negatives = n_negatives
        self.sample_with_prior = sample_with_prior
        self.sample_with_posterior = sample_with_posterior

        self.save_hyperparameters("freeze_bert", "n_negatives", "valid_n_negatives",
                                  "lr", "weight_decay", "replacement", "do_validation")
        for name in self.hparams:
            setattr(self, name, getattr(self.hparams, name))
        self.training_prior_fcn = training_prior_fcn

        self.item_tower = _Tower(
            _create_bert(model_name, freeze_bert),
            torch.nn.LayerNorm(768, elementwise_affine=elementwise_affine),  # TODO: other transform layers
        )
        self.all_inputs = all_inputs

        if pretrained_checkpoint is not None:
            self.load_state_dict(torch.load(pretrained_checkpoint))

    def set_training_data(self, i_to_ptr=None, j_to_ptr=None, prior_score=None, item_freq=None):
        self.register_buffer("i_to_ptr", torch.as_tensor(i_to_ptr), False)
        self.register_buffer("j_to_ptr", torch.as_tensor(j_to_ptr), False)
        if prior_score is not None and self.sample_with_prior:
            self.register_buffer("tr_prior_score", sps_to_torch(prior_score, 'cpu'), False)
        if item_freq is not None:
            item_proposal = (item_freq + 0.1) ** self.sample_with_posterior
            self.register_buffer("tr_item_proposal", torch.as_tensor(item_proposal), False)

    def setup(self, stage):  # auto-call in fit loop
        if stage == 'fit':
            print(self._checkpoint.dirpath)

    def forward(self, batch):  # tokenized or ptr
        output_step = getattr(self, "override_output_step", "final")
        if isinstance(batch, collections.abc.Mapping):  # tokenized
            return self.item_tower(**batch, output_step=output_step)
        elif hasattr(self, 'all_cls'):  # ptr
            return self.item_tower(self.all_cls[batch], input_step='cls', output_step=output_step)
        else:  # ptr to all_inputs
            return self.item_tower(**{k: v[batch] for k, v in self.all_inputs.items()},
                                   output_step=output_step)

    def _pairwise(self, i, j):  # auto-broadcast on first dimension
        x = self.forward(self.i_to_ptr[i.ravel()]).reshape([*i.shape, -1])
        y = self.forward(self.j_to_ptr[j.ravel()]).reshape([*j.shape, -1])
        return (x * y).sum(-1)

    def training_and_validation_step(self, batch, batch_idx):
        i, j, w = batch.T
        i = i.to(int)
        j = j.to(int)
        pos_score = self._pairwise(i, j)  # bsz

        n_negatives = self.n_negatives if self.training else self.valid_n_negatives
        n_shape = (n_negatives, len(batch))
        loglik = []

        with torch.no_grad():
            if hasattr(self, "tr_prior_score"):
                if hasattr(self.tr_prior_score, "as_tensor"):
                    prior_score = self.tr_prior_score[i.tolist()].as_tensor(i.device)
                else:
                    prior_score = self.tr_prior_score.index_select(0, i).to_dense()
                nj = torch.multinomial(
                    (self.training_prior_fcn(prior_score) + self.tr_item_proposal.log()).softmax(1),
                    n_negatives, self.replacement).T
            else:
                nj = torch.multinomial(self.tr_item_proposal, np.prod(n_shape), self.replacement).reshape(n_shape)
        nj_score = self._pairwise(i, nj)
        loglik.append(F.logsigmoid(pos_score - nj_score))  # nsamp * bsz

        return (-torch.stack(loglik) * w).sum() / (len(loglik) * n_negatives * w.sum())

    def configure_optimizers(self):
        if self.do_validation:
            optimizer = torch.optim.Adagrad(
                self.parameters(), eps=1e-3, lr=self.lr, weight_decay=self.weight_decay)
            lr_scheduler = _ReduceLRLoadCkpt(
                optimizer, model=self, factor=0.25, patience=4, verbose=True)
            return {"optimizer": optimizer, "lr_scheduler": {
                    "scheduler": lr_scheduler, "monitor": "val_epoch_loss"
                    }}
        else:
            return torch.optim.Adagrad(self.parameters(),
                                       eps=1e-3, lr=self.lr, weight_decay=self.weight_decay)


class _DataModule(LightningDataModule):
    def __init__(self, rime_dataset, item_index=None, item_tokenized=None, do_validation=None,
                 batch_size=None, valid_batch_size=None, predict_batch_size=None):
        super().__init__()
        self._D = rime_dataset
        self._item_tokenized = item_tokenized
        self._do_validation = do_validation
        self._batch_size = batch_size
        self._valid_batch_size = valid_batch_size
        self._predict_batch_size = predict_batch_size
        self._num_batches = self._D.target_csr.nnz / self._batch_size

        item_to_ptr = {k: ptr for ptr, k in enumerate(item_index)}
        self.i_to_ptr = [item_to_ptr[hist[0]] for hist in self._D.user_in_test['_hist_items']]
        self.j_to_ptr = [item_to_ptr[item] for item in self._D.item_in_test.index]
        self.i_to_item_id = np.array(item_index)[self.i_to_ptr]
        self.j_to_item_id = np.array(item_index)[self.j_to_ptr]
        self.training_data = {
            'i_to_ptr': self.i_to_ptr, 'j_to_ptr': self.j_to_ptr,
            'prior_score': self._D.prior_score,
            'item_freq': self._D.item_in_test['_hist_len'].values}

    def setup(self, stage):  # auto-call by trainer
        if stage == 'fit':
            target_coo = self._D.target_csr.tocoo()
            dataset = np.transpose([target_coo.row, target_coo.col, target_coo.data])
            self._num_workers = (len(dataset) > 1e4) * 4

            if self._do_validation:
                self._train_set, self._valid_set = default_random_split(dataset)
            else:
                self._train_set, self._valid_set = dataset, None
            print("train_set size", len(self._train_set))

    def train_dataloader(self):
        return DataLoader(self._train_set, self._batch_size, num_workers=self._num_workers, shuffle=True)

    def val_dataloader(self):
        if self._do_validation:
            return DataLoader(self._valid_set, self._valid_batch_size, num_workers=self._num_workers)

    def predict_dataloader(self):
        dataset = Dataset.from_dict(self._item_tokenized)
        return DataLoader(dataset, batch_size=self._predict_batch_size,
                          num_workers=(len(dataset) > 1000) * 4,
                          collate_fn=DefaultDataCollator())


class BertBPR:
    def __init__(self, item_df, freeze_bert=0, batch_size=None,
                 model_name='bert-base-uncased', max_length=128,
                 max_epochs=10, max_steps=-1, do_validation=None,
                 strategy='dp', query_item_position_in_user_history=0,
                 **kw):
        if batch_size is None:
            batch_size = 10000 if freeze_bert > 0 else 10
        if do_validation is None:
            do_validation = max_epochs > 1

        self.item_titles = item_df['TITLE']
        self._model_kw = {"freeze_bert": freeze_bert, "do_validation": do_validation, **kw}
        self.max_length = max_length
        self.batch_size = batch_size
        self.do_validation = do_validation
        self.max_epochs = max_epochs
        self.max_steps = max_steps
        self.strategy = strategy

        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.all_inputs = self.tokenizer(
            self.item_titles.tolist(), padding='max_length', return_tensors='pt',
            max_length=self.max_length, truncation=True
        )
        self.model = _BertBPR(self.all_inputs, **self._model_kw)
        self.valid_batch_size = self.batch_size * self.model.n_negatives * 2 // self.model.valid_n_negatives
        self.predict_batch_size = 64 * torch.cuda.device_count()

        self._ckpt_dirpath = []
        self._logger = TensorBoardLogger('logs', "BertBPR")
        self._logger.log_hyperparams({k: v for k, v in locals().items() if k in [
            'freeze_bert', 'batch_size', 'max_epochs', 'max_steps', 'sample_with_prior', 'sample_with_posterior'
        ]})
        print(f'BertBPR logs at {self._logger.log_dir}')

    def _get_data_module(self, V):
        return _DataModule(V, self.item_titles.index, self.all_inputs, self.do_validation,
                           self.batch_size, self.valid_batch_size, self.predict_batch_size)

    @empty_cache_on_exit
    def fit(self, V=None, _lr_find=False):
        if V is None or not any([param.requires_grad for param in self.model.parameters()]):
            return self
        model = _BertBPR(self.all_inputs, **self._model_kw)
        dm = self._get_data_module(V)
        model.set_training_data(**dm.training_data)
        trainer = Trainer(
            max_epochs=self.max_epochs, max_steps=self.max_steps,
            gpus=torch.cuda.device_count(), strategy=self.strategy,
            log_every_n_steps=1, callbacks=[model._checkpoint, LearningRateMonitor()])

        if self._model_kw['freeze_bert'] > 0:  # cache all_cls
            model.override_output_step = 'cls'
            all_cls = trainer.predict(model, datamodule=dm)
            model.register_buffer("all_cls", torch.cat(all_cls), False)
            del model.override_output_step  # restore to final

        if _lr_find:
            lr_finder = trainer.tuner.lr_find(model, datamodule=dm,
                                              min_lr=1e-4, early_stop_threshold=None)
            fig = lr_finder.plot(suggest=True)
            fig.show()
            return lr_finder, lr_finder.suggestion()

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

    @empty_cache_on_exit
    @torch.no_grad()
    def transform(self, D):
        dm = self._get_data_module(D)
        trainer = Trainer(gpus=torch.cuda.device_count(), strategy=self.strategy)
        all_emb = trainer.predict(self.model, datamodule=dm)
        all_emb = torch.cat(all_emb)

        user_final = all_emb[dm.i_to_ptr]
        item_final = all_emb[dm.j_to_ptr]
        return auto_cast_lazy_score(user_final) @ item_final.T

    def to_explainer(self, **kw):
        tower = self.model.item_tower.to(auto_device()).eval()
        return I2IExplainer(tower, self.tokenizer, **kw)
