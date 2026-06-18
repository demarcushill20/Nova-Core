"""Phase D1 — DreamerV3 world model (Recurrent State-Space Model).

Compact, torch-only, auditable reference implementation of the RSSM used by
DreamerV3. The model is *categorical*: the stochastic latent ``z`` is a set of
``stoch_dim`` independent categorical variables with ``num_categories`` classes
each (straight-through one-hot sample), exactly as in the DreamerV3 paper.

State at step t:
    deter h_t  : GRU recurrent state (deterministic memory)
    stoch z_t  : categorical latent (one-hot, flattened)

Transitions:
    h_t      = GRU(h_{t-1}, MLP([z_{t-1}, a_{t-1}]))
    prior    : p(z_t | h_t)              -- "imagination" / dynamics
    post     : q(z_t | h_t, embed(x_t))  -- uses the real observation

Decoders (heads) read [h_t, z_t]:
    obs head      : reconstruct the 152-dim feature vector (MSE on symlog)
    reward head   : two-hot / symlog distributional reward
    continue head : Bernoulli (episode continuation)

The two-hot + symlog machinery and KL balancing / free bits live in
``agent.py`` losses; this file owns the network and the rollout primitives.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


def symlog(x: torch.Tensor) -> torch.Tensor:
    return torch.sign(x) * torch.log1p(torch.abs(x))


def symexp(x: torch.Tensor) -> torch.Tensor:
    return torch.sign(x) * torch.expm1(torch.abs(x))


def _mlp(sizes: list[int], act=nn.SiLU) -> nn.Sequential:
    layers: list[nn.Module] = []
    for i in range(len(sizes) - 1):
        layers.append(nn.Linear(sizes[i], sizes[i + 1]))
        if i < len(sizes) - 2:
            layers.append(nn.LayerNorm(sizes[i + 1]))
            layers.append(act())
    return nn.Sequential(*layers)


class RSSM(nn.Module):
    def __init__(
        self,
        obs_dim: int,
        n_actions: int,
        embed_dim: int = 256,
        hidden_dim: int = 512,
        deter_dim: int = 512,
        stoch_dim: int = 32,
        num_categories: int = 32,
        unimix: float = 0.01,
    ) -> None:
        super().__init__()
        self.obs_dim = obs_dim
        self.n_actions = n_actions
        self.deter_dim = deter_dim
        self.stoch_dim = stoch_dim
        self.num_categories = num_categories
        self.stoch_flat = stoch_dim * num_categories
        self.unimix = unimix

        # Observation encoder.
        self.encoder = _mlp([obs_dim, hidden_dim, embed_dim])

        # Recurrent core: GRU over a projection of [z_{t-1}, a_{t-1}].
        self.pre_gru = _mlp([self.stoch_flat + n_actions, hidden_dim])
        self.gru = nn.GRUCell(hidden_dim, deter_dim)

        # Prior p(z|h) and posterior q(z|h, embed).
        self.prior_net = _mlp([deter_dim, hidden_dim, self.stoch_flat])
        self.post_net = _mlp([deter_dim + embed_dim, hidden_dim, self.stoch_flat])

        feat_dim = deter_dim + self.stoch_flat
        # Decoders.
        self.obs_head = _mlp([feat_dim, hidden_dim, obs_dim])
        self.reward_head = _mlp([feat_dim, hidden_dim, 255])  # two-hot bins
        self.cont_head = _mlp([feat_dim, hidden_dim, 1])

    # ------------------------------------------------------------------ #
    def _logits(self, net: nn.Module, x: torch.Tensor) -> torch.Tensor:
        logits = net(x).view(*x.shape[:-1], self.stoch_dim, self.num_categories)
        # unimix: blend with uniform to keep gradients alive (DreamerV3).
        probs = F.softmax(logits, dim=-1)
        probs = (1 - self.unimix) * probs + self.unimix / self.num_categories
        return torch.log(probs)

    def _sample(self, logits: torch.Tensor) -> torch.Tensor:
        """Straight-through one-hot categorical sample, flattened."""
        dist = torch.distributions.OneHotCategorical(logits=logits)
        sample = dist.sample()
        probs = F.softmax(logits, dim=-1)
        sample = sample + probs - probs.detach()  # straight-through
        return sample.view(*logits.shape[:-2], self.stoch_flat)

    def initial(self, batch: int, device) -> tuple[torch.Tensor, torch.Tensor]:
        h = torch.zeros(batch, self.deter_dim, device=device)
        z = torch.zeros(batch, self.stoch_flat, device=device)
        return h, z

    def img_step(self, h, z, action):
        """One *imagined* step: advance h, sample z from the prior."""
        x = torch.cat([z, action], dim=-1)
        h = self.gru(self.pre_gru(x), h)
        prior_logits = self._logits(self.prior_net, h)
        z = self._sample(prior_logits)
        return h, z, prior_logits

    def obs_step(self, h, z, action, embed):
        """One *observed* step: advance h, sample z from the posterior."""
        x = torch.cat([z, action], dim=-1)
        h = self.gru(self.pre_gru(x), h)
        prior_logits = self._logits(self.prior_net, h)
        post_logits = self._logits(self.post_net, torch.cat([h, embed], dim=-1))
        z = self._sample(post_logits)
        return h, z, prior_logits, post_logits

    def feat(self, h, z) -> torch.Tensor:
        return torch.cat([h, z], dim=-1)

    def observe(self, obs_seq, action_seq):
        """Roll the posterior over a sequence.

        obs_seq:    (B, T, obs_dim)
        action_seq: (B, T, n_actions)  one-hot actions taken *into* each step.
        Returns dict of stacked (B, T, ...) tensors + final (h, z).
        """
        b, t, _ = obs_seq.shape
        device = obs_seq.device
        embed = self.encoder(obs_seq)  # (B,T,embed)
        h, z = self.initial(b, device)
        hs, zs, priors, posts = [], [], [], []
        for i in range(t):
            h, z, prior_logits, post_logits = self.obs_step(h, z, action_seq[:, i], embed[:, i])
            hs.append(h)
            zs.append(z)
            priors.append(prior_logits)
            posts.append(post_logits)
        return {
            "h": torch.stack(hs, 1),
            "z": torch.stack(zs, 1),
            "prior_logits": torch.stack(priors, 1),
            "post_logits": torch.stack(posts, 1),
        }
