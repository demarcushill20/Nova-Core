"""Phase D2 — DreamerV3 actor-critic + imagination + world-model losses.

Owns everything that turns the RSSM (rssm.py) into a trainable agent:

    * two-hot / symlog distributional heads for reward and value
    * world-model loss (recon + reward + continue + KL-balanced free-bits)
    * actor + critic networks over the model state ``feat = [h, z]``
    * imagination rollout (horizon=15) and lambda-return actor-critic update
    * a flat replay buffer of real (obs, action, reward, cont) transitions

Kept compact and torch-only so the whole learning loop is auditable in one
file. Defaults follow the task spec (§17): embed 256, hidden 512, stoch 32x32,
gamma 0.99, lambda 0.95, horizon 15.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .rssm import RSSM, _mlp, symexp, symlog

# Two-hot support: 255 bins spanning symlog space [-20, 20].
N_BINS = 255
BIN_LO, BIN_HI = -20.0, 20.0


def _bins(device) -> torch.Tensor:
    return torch.linspace(BIN_LO, BIN_HI, N_BINS, device=device)


def two_hot(x: torch.Tensor, bins: torch.Tensor) -> torch.Tensor:
    """Encode symlog(x) as a two-hot vector over ``bins``. x: (...,) -> (...,N)."""
    x = symlog(x).clamp(BIN_LO, BIN_HI)
    idx = torch.bucketize(x, bins).clamp(1, len(bins) - 1)
    lo = bins[idx - 1]
    hi = bins[idx]
    w_hi = (x - lo) / (hi - lo + 1e-8)
    oh = torch.zeros(*x.shape, len(bins), device=x.device)
    oh.scatter_(-1, idx.unsqueeze(-1), w_hi.unsqueeze(-1))
    oh.scatter_(-1, (idx - 1).unsqueeze(-1), (1 - w_hi).unsqueeze(-1))
    return oh


def from_logits(logits: torch.Tensor, bins: torch.Tensor) -> torch.Tensor:
    """Expected value (in raw space) of a two-hot distribution given logits."""
    probs = F.softmax(logits, dim=-1)
    return symexp((probs * bins).sum(-1))


@dataclass
class DreamerLosses:
    world: float
    actor: float
    critic: float
    recon: float
    reward: float
    kl: float


class ReplayBuffer:
    """Flat ring buffer of single-step transitions; samples length-L sequences."""

    def __init__(self, capacity: int, obs_dim: int, n_actions: int) -> None:
        self.capacity = capacity
        self.obs = np.zeros((capacity, obs_dim), dtype=np.float32)
        self.act = np.zeros((capacity, n_actions), dtype=np.float32)
        self.rew = np.zeros((capacity,), dtype=np.float32)
        self.cont = np.zeros((capacity,), dtype=np.float32)
        self.size = 0
        self.ptr = 0

    def add(self, obs, action_onehot, reward, cont) -> None:
        i = self.ptr
        self.obs[i] = obs
        self.act[i] = action_onehot
        self.rew[i] = reward
        self.cont[i] = cont
        self.ptr = (self.ptr + 1) % self.capacity
        self.size = min(self.size + 1, self.capacity)

    def sample(self, batch: int, length: int, device):
        hi = self.size - length
        starts = np.random.randint(0, hi, size=batch)
        bidx = starts[:, None] + np.arange(length)[None, :]

        def to(a):
            return torch.as_tensor(a[bidx], device=device)

        return to(self.obs), to(self.act), to(self.rew), to(self.cont)


class Dreamer(nn.Module):
    def __init__(
        self,
        obs_dim: int,
        n_actions: int,
        embed_dim: int = 256,
        hidden_dim: int = 512,
        stoch_dim: int = 32,
        num_categories: int = 32,
        gamma: float = 0.99,
        lam: float = 0.95,
        horizon: int = 15,
        free_bits: float = 1.0,
        kl_balance: float = 0.8,
        device: str = "cpu",
    ) -> None:
        super().__init__()
        self.device = torch.device(device)
        self.n_actions = n_actions
        self.gamma = gamma
        self.lam = lam
        self.horizon = horizon
        self.free_bits = free_bits
        self.kl_balance = kl_balance

        self.world = RSSM(
            obs_dim,
            n_actions,
            embed_dim,
            hidden_dim,
            deter_dim=hidden_dim,
            stoch_dim=stoch_dim,
            num_categories=num_categories,
        )
        feat_dim = self.world.deter_dim + self.world.stoch_flat
        self.actor = _mlp([feat_dim, hidden_dim, n_actions])
        self.critic = _mlp([feat_dim, hidden_dim, N_BINS])
        self.critic_slow = _mlp([feat_dim, hidden_dim, N_BINS])
        self.critic_slow.load_state_dict(self.critic.state_dict())

        self.world_opt = torch.optim.Adam(self.world.parameters(), lr=1e-4, eps=1e-8)
        self.actor_opt = torch.optim.Adam(self.actor.parameters(), lr=3e-5, eps=1e-8)
        self.critic_opt = torch.optim.Adam(self.critic.parameters(), lr=3e-5, eps=1e-8)
        self.register_buffer("bins", _bins(self.device))
        self.to(self.device)

    # ------------------------------------------------------------------ #
    def _kl(self, post_logits, prior_logits):
        """KL-balanced categorical KL with free bits (DreamerV3)."""

        def kl(p_logits, q_logits):
            p = torch.distributions.Categorical(logits=p_logits)
            q = torch.distributions.Categorical(logits=q_logits)
            return torch.distributions.kl_divergence(p, q).sum(-1)

        kl_dyn = kl(post_logits.detach(), prior_logits)  # train prior
        kl_rep = kl(post_logits, prior_logits.detach())  # train posterior
        kl_dyn = torch.clamp(kl_dyn, min=self.free_bits).mean()
        kl_rep = torch.clamp(kl_rep, min=self.free_bits).mean()
        return self.kl_balance * kl_dyn + (1 - self.kl_balance) * kl_rep

    def world_loss(self, obs, act, rew, cont):
        out = self.world.observe(obs, act)
        feat = self.world.feat(out["h"], out["z"])
        # Reconstruction in symlog space.
        recon = F.mse_loss(self.world.obs_head(feat), symlog(obs))
        # Reward (two-hot) + continue (bernoulli).
        rew_logits = self.world.reward_head(feat)
        reward_loss = -(two_hot(rew, self.bins) * F.log_softmax(rew_logits, -1)).sum(-1).mean()
        cont_logits = self.world.cont_head(feat).squeeze(-1)
        cont_loss = F.binary_cross_entropy_with_logits(cont_logits, cont)
        kl = self._kl(out["post_logits"], out["prior_logits"])
        loss = recon + reward_loss + cont_loss + kl
        return loss, out, recon.item(), reward_loss.item(), kl.item()

    def imagine(self, h0, z0):
        """Roll the actor through the prior for ``horizon`` steps (detached start)."""
        h, z = h0.detach(), z0.detach()
        feats, actions, entropies = [], [], []
        for _ in range(self.horizon):
            feat = self.world.feat(h, z)
            logits = self.actor(feat)
            dist = torch.distributions.OneHotCategorical(logits=logits)
            a = dist.sample()
            a = a + F.softmax(logits, -1) - F.softmax(logits, -1).detach()  # straight-through
            feats.append(feat)
            actions.append(a)
            entropies.append(dist.entropy())
            h, z, _ = self.world.img_step(h, z, a)
        feats.append(self.world.feat(h, z))
        return torch.stack(feats), torch.stack(actions), torch.stack(entropies)

    def _lambda_return(self, rewards, values, gamma):
        """Discounted lambda-returns over the imagination horizon."""
        ret = torch.zeros_like(values)
        ret[-1] = values[-1]
        for t in reversed(range(len(rewards))):
            ret[t] = rewards[t] + gamma * ((1 - self.lam) * values[t + 1] + self.lam * ret[t + 1])
        return ret

    def actor_critic_loss(self, out):
        img_feat, img_act, img_ent = self.imagine(
            out["h"].reshape(-1, out["h"].shape[-1]), out["z"].reshape(-1, out["z"].shape[-1])
        )
        # Predicted reward + value along imagined trajectory.
        rew = from_logits(self.world.reward_head(img_feat), self.bins)
        val = from_logits(self.critic(img_feat), self.bins)
        with torch.no_grad():
            val_slow = from_logits(self.critic_slow(img_feat), self.bins)
        ret = self._lambda_return(rew[:-1], val_slow, self.gamma)  # (H, N)

        # Critic: regress two-hot(returns) at imagined states (detached targets).
        critic_logits = self.critic(img_feat[:-1].detach())
        target = two_hot(ret[:-1].detach(), self.bins)
        critic_loss = -(target * F.log_softmax(critic_logits, -1)).sum(-1).mean()

        # Actor: maximise advantage (return - value), with entropy bonus.
        adv = (ret[:-1] - val[:-1]).detach()
        logits = self.actor(img_feat[:-1])
        logp = (F.log_softmax(logits, -1) * img_act).sum(-1)
        actor_loss = -(logp * adv).mean() - 3e-4 * img_ent.mean()
        return actor_loss, critic_loss

    def train_step(self, batch) -> DreamerLosses:
        obs, act, rew, cont = batch
        # World model.
        w_loss, out, recon, reward_l, kl = self.world_loss(obs, act, rew, cont)
        self.world_opt.zero_grad()
        w_loss.backward()
        nn.utils.clip_grad_norm_(self.world.parameters(), 100.0)
        self.world_opt.step()

        # Actor-critic on imagined rollouts (world frozen here).
        detached = {k: v.detach() for k, v in out.items()}
        a_loss, c_loss = self.actor_critic_loss(detached)
        self.actor_opt.zero_grad()
        a_loss.backward()
        nn.utils.clip_grad_norm_(self.actor.parameters(), 100.0)
        self.actor_opt.step()
        self.critic_opt.zero_grad()
        c_loss.backward()
        nn.utils.clip_grad_norm_(self.critic.parameters(), 100.0)
        self.critic_opt.step()

        # Slow critic EMA.
        with torch.no_grad():
            for p, ps in zip(self.critic.parameters(), self.critic_slow.parameters(), strict=True):
                ps.mul_(0.98).add_(0.02 * p)
        return DreamerLosses(w_loss.item(), a_loss.item(), c_loss.item(), recon, reward_l, kl)

    # ------------------------------------------------------------------ #
    @torch.no_grad()
    def act(self, h, z, greedy: bool = True):
        """Policy on a single live model state. Returns (action_idx, h, z)."""
        feat = self.world.feat(h, z)
        logits = self.actor(feat)
        a = logits.argmax(-1) if greedy else torch.distributions.OneHotCategorical(logits=logits).sample().argmax(-1)
        return int(a.item())
