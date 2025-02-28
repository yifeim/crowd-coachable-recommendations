import math
from typing import Dict, List, Optional, Set, Tuple, Union

from transformers.configuration_utils import PretrainedConfig
from transformers import DistilBertPreTrainedModel, DistilBertModel
from transformers.activations import get_activation
from transformers.modeling_outputs import MaskedLMOutput

import torch
from torch import nn
from torch.nn import CrossEntropyLoss
import copy

class EmbeddingModel(DistilBertPreTrainedModel):
    def __init__(self, config: PretrainedConfig):
        super().__init__(config)

        self.activation = get_activation(config.activation)

        self.distilbert = DistilBertModel(config)
        self.vocab_transform = nn.Linear(config.dim, config.dim)
        self.vocab_layer_norm = nn.LayerNorm(config.dim, eps=1e-12)
        self.vocab_projector = nn.Linear(config.dim, config.vocab_size)

        # Initialize weights and apply final processing
        self.post_init()

        self.standard_layer_norm = nn.LayerNorm(config.dim, eps=1e-12)

        self.loss_fct = nn.CrossEntropyLoss()

    def generate_mean(self,hidden_states):
        raise NotImplementedError("return type: torch.Tensor")
    
    def generate_std(self,hidden_states):
        raise NotImplementedError("return type: float or torch.Tensor")
    
    def compute_output_loss(self, mu, std, prediction_logits, input_ids,labels):
        raise NotImplementedError("return type: torch.Tensor")
    
    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        labels: Optional[torch.LongTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_mean_std: Optional[bool] = False,
        return_embedding: Optional[bool] = False,
        return_dict: Optional[bool] = None
    ) -> Union[MaskedLMOutput, Tuple[torch.Tensor, ...]]:

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        dlbrt_output = self.distilbert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        
        seq_length = dlbrt_output[0].size(dim = 1)
        hidden_states = dlbrt_output[0][:,0,:]  # (bs, dim)

        mu = self.generate_mean(hidden_states)
        std = self.generate_std(hidden_states)

        if return_mean_std:
            return mu, std

        eps = torch.randn_like(mu)
        hidden_states = eps * std + mu
        
        hidden_states = self.vocab_transform(hidden_states)  # (bs, dim)
        hidden_states = self.activation(hidden_states)  # (bs, dim)

        #use standard_layer_norm to avoid using the weights in the trained layer_norm and keep the norm of the embedding as a constant
        if return_embedding:
            return self.standard_layer_norm(hidden_states) 
        
        prediction_logits = self.vocab_layer_norm(hidden_states)  # (bs, dim)
        prediction_logits = self.vocab_projector(prediction_logits)  # (bs, vocab_size)
        
        bs = prediction_logits.size(dim = 0)
        vocab_size = prediction_logits.size(dim = 1)
        
        prediction_logits = torch.reshape(prediction_logits,(bs,1,vocab_size))
        prediction_logits = prediction_logits.repeat(1,seq_length,1)

        output_loss = self.compute_output_loss(mu, std, prediction_logits, input_ids, labels)

        return MaskedLMOutput(
            loss=output_loss,
            logits=prediction_logits,
            hidden_states=dlbrt_output.hidden_states,
            attentions=dlbrt_output.attentions,
        )


class MaskedPretrainedModel(EmbeddingModel):
    def __init__(self, config: PretrainedConfig):
        super().__init__(config)
        self.std = 0.0

    def generate_mean(self,hidden_states):
        return hidden_states
    
    def generate_std(self,hidden_states):
        return self.std
    
    def compute_output_loss(self, mu, std, prediction_logits, input_ids, labels):
        return self.loss_fct(prediction_logits.view(-1, prediction_logits.size(-1)), labels.view(-1)) 
    

class VAEPretrainedModel(EmbeddingModel):
    def __init__(self, config: PretrainedConfig):
        super().__init__(config)
        self.fc_mu = nn.Linear(config.dim, config.dim)
        self.fc_var = nn.Linear(config.dim, config.dim)

        self.vae_beta = 1e-5
    
    def VAE_post_init(self):
        dim = self.fc_var.weight.size(1)

        #intialize fc_mu to be identity
        self.fc_mu.bias.data.zero_()
        self.fc_mu.weight.data = torch.eye(dim)

        #initialize fc_var according to prior
        var_init = 0.01
        stdv = var_init / math.sqrt(dim)
        self.fc_var.weight.data.uniform_(-stdv, stdv)
        self.fc_var.bias.data.zero_()
    
    def set_beta(self, beta):
        self.vae_beta = beta

    def generate_mean(self,hidden_states):
        return self.fc_mu(hidden_states)
    
    def generate_std(self,hidden_states):
        log_var = self.fc_var(hidden_states)
        return torch.exp(0.5 * log_var)
    
    def compute_output_loss(self, mu, std, prediction_logits, input_ids, labels):

        recon_loss = self.loss_fct(prediction_logits.view(-1, prediction_logits.size(-1)), input_ids.view(-1))
        kld_loss = torch.mean(-0.5 * torch.sum(1 + 2 * torch.log(std) - mu ** 2 - std ** 2, dim = 1), dim = 0)

        return recon_loss + self.vae_beta * kld_loss
