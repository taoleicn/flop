'''
Code taken from Transformer-XL
https://github.com/kimiyoung/transformer-xl
'''

from collections import defaultdict

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

class AdaptiveEmbedding(nn.Module):
    def __init__(self, n_token, d_embed, d_proj, cutoffs, div_val=1,
                 sample_softmax=False):
        super(AdaptiveEmbedding, self).__init__()

        self.n_token = n_token
        self.d_embed = d_embed

        self.cutoffs = cutoffs + [n_token]
        self.div_val = div_val
        self.d_proj = d_proj

        self.emb_scale = d_proj ** 0.5

        self.cutoff_ends = [0] + self.cutoffs

        self.emb_layers = nn.ModuleList()
        self.emb_projs = nn.ParameterList()
        for i in range(len(self.cutoffs)):
            l_idx, r_idx = self.cutoff_ends[i], self.cutoff_ends[i+1]
            d_emb_i = int(d_embed // (div_val ** i))
            self.emb_layers.append(nn.Embedding(r_idx-l_idx, d_emb_i))
            self.emb_projs.append(nn.Parameter(torch.Tensor(d_proj, d_emb_i)))

    def forward(self, inp):
        param = next(self.parameters())
        inp_flat = inp.view(-1)
        emb_flat = torch.zeros([inp_flat.size(0), self.d_proj],
            dtype=param.dtype, device=param.device)
        for i in range(len(self.cutoffs)):
            l_idx, r_idx = self.cutoff_ends[i], self.cutoff_ends[i + 1]

            mask_i = (inp_flat >= l_idx) & (inp_flat < r_idx)
            indices_i = mask_i.nonzero().squeeze()

            if indices_i.numel() == 0:
                continue

            inp_i = inp_flat.index_select(0, indices_i) - l_idx
            emb_i = self.emb_layers[i](inp_i)
            emb_i = F.linear(emb_i, self.emb_projs[i])

            emb_flat.index_copy_(0, indices_i, emb_i)

        embed = emb_flat.view(*inp.size(), self.d_proj)

        embed.mul_(self.emb_scale)

        return embed


class AdaptiveLogSoftmax(nn.Module):
    def __init__(self, n_token, d_embed, d_proj, cutoffs, div_val=1,
                 keep_order=False):
        super(AdaptiveLogSoftmax, self).__init__()

        self.n_token = n_token
        self.d_embed = d_embed

        self.cutoffs = cutoffs + [n_token]
        self.cutoff_ends = [0] + self.cutoffs
        self.div_val = div_val

        self.shortlist_size = self.cutoffs[0]
        self.n_clusters = len(self.cutoffs) - 1
        self.head_size = self.shortlist_size + self.n_clusters

        self.out_layers = nn.ModuleList()
        self.out_projs = nn.ParameterList()

        for i in range(len(self.cutoffs)):
            l_idx, r_idx = self.cutoff_ends[i], self.cutoff_ends[i+1]
            d_emb_i = int(d_embed // (div_val ** i))
            output_size_i = r_idx - l_idx if i > 0 else (r_idx - l_idx) + self.n_clusters

            self.out_projs.append(
                nn.Parameter(torch.Tensor(d_proj, d_emb_i))
            )
            self.out_layers.append(nn.Linear(d_emb_i, output_size_i))

        self.keep_order = keep_order

    def _compute_logit(self, hidden, weight, bias, proj):
        if proj is None:
            logit = F.linear(hidden, weight, bias=bias)
        else:
            proj_hid = F.linear(hidden, proj.t().contiguous())
            logit = F.linear(proj_hid, weight, bias=bias)

        return logit

    def forward(self, hidden, target, keep_order=False):
        '''
            hidden :: [len*bsz x d_proj]
            target :: [len*bsz]
        '''

        if hidden.size(0) != target.size(0):
            raise RuntimeError('Input and target should have the same size '
                               'in the batch dimension.')

        # construct weights and biases
        weights = [l.weight for l in self.out_layers]
        biases = [l.bias for l in self.out_layers]
        out_projs = self.out_projs

        head_weight, head_bias, head_proj = weights[0], biases[0], out_projs[0]

        head_logit = self._compute_logit(hidden, head_weight, head_bias, head_proj)
        head_logprob = F.log_softmax(head_logit, dim=1)

        if self.n_clusters == 0:
            return -head_logprob.gather(1, target.unsqueeze(1)).squeeze(1)

        nll = torch.zeros_like(target,
                dtype=hidden.dtype, device=hidden.device)

        offset = 0
        cutoff_values = [0] + self.cutoffs
        for i in range(len(cutoff_values) - 1):
            l_idx, r_idx = cutoff_values[i], cutoff_values[i + 1]

            mask_i = (target >= l_idx) & (target < r_idx)
            indices_i = mask_i.nonzero().squeeze()

            if indices_i.numel() == 0:
                continue

            target_i = target.index_select(0, indices_i) - l_idx
            head_logprob_i = head_logprob.index_select(0, indices_i)

            if i == 0:
                logprob_i = head_logprob_i.gather(1, target_i[:,None]).squeeze(1)
            else:
                weight_i, bias_i, proj_i = weights[i], biases[i], out_projs[i]

                hidden_i = hidden.index_select(0, indices_i)

                tail_logit_i = self._compute_logit(hidden_i, weight_i, bias_i, proj_i)
                tail_logprob_i = F.log_softmax(tail_logit_i, dim=1)

                logprob_i = head_logprob_i[:, -i] \
                          + tail_logprob_i.gather(1, target_i[:,None]).squeeze(1)

            if (hasattr(self, 'keep_order') and self.keep_order) or keep_order:
                nll.index_copy_(0, indices_i, -logprob_i)
            else:
                nll[offset:offset+logprob_i.size(0)].copy_(-logprob_i)

            offset += logprob_i.size(0)

        return nll