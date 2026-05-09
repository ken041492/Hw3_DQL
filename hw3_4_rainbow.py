import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import IterableDataset, DataLoader
import pytorch_lightning as pl
import random
import copy
from collections import deque
from matplotlib import pylab as plt
from Gridworld import Gridworld

GRID_SIZE = 4
STATE_SIZE = 4 * GRID_SIZE * GRID_SIZE
MAX_STEPS = 1000000
BATCH_SIZE = 256
N_STEP = 3

action_set = {
    0: 'u',
    1: 'd',
    2: 'l',
    3: 'r',
}

class NoisyLinear(nn.Module):
    def __init__(self, in_features, out_features, std_init=0.5):
        super(NoisyLinear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.std_init = std_init
        
        self.weight_mu = nn.Parameter(torch.empty(out_features, in_features))
        self.weight_sigma = nn.Parameter(torch.empty(out_features, in_features))
        self.register_buffer('weight_epsilon', torch.empty(out_features, in_features))
        
        self.bias_mu = nn.Parameter(torch.empty(out_features))
        self.bias_sigma = nn.Parameter(torch.empty(out_features))
        self.register_buffer('bias_epsilon', torch.empty(out_features))
        
        self.reset_parameters()
        self.reset_noise()

    def reset_parameters(self):
        mu_range = 1 / math.sqrt(self.in_features)
        self.weight_mu.data.uniform_(-mu_range, mu_range)
        self.weight_sigma.data.fill_(self.std_init / math.sqrt(self.in_features))
        self.bias_mu.data.uniform_(-mu_range, mu_range)
        self.bias_sigma.data.fill_(self.std_init / math.sqrt(self.out_features))

    def _scale_noise(self, size):
        x = torch.randn(size)
        return x.sign().mul_(x.abs().sqrt_())

    def reset_noise(self):
        epsilon_in = self._scale_noise(self.in_features)
        epsilon_out = self._scale_noise(self.out_features)
        self.weight_epsilon.copy_(epsilon_out.outer(epsilon_in))
        self.bias_epsilon.copy_(epsilon_out)

    def forward(self, x):
        if self.training:
            weight = self.weight_mu + self.weight_sigma * self.weight_epsilon
            bias = self.bias_mu + self.bias_sigma * self.bias_epsilon
        else:
            weight = self.weight_mu
            bias = self.bias_mu
        return F.linear(x, weight, bias)


# 為什麼在 8x8 或是全隨機的環境中，Rainbow 需要更深的神經網路與更長的訓練步數？
# 1. 狀態空間龐大：物件位置全隨機導致可能的狀態組合呈指數增長，淺層網路無法捕捉所有特徵。
# 2. C51 的計算負擔：網路必須預測每一個動作的 51 個分佈機率，而不僅僅是一個單一期望值，輸出維度大且分佈精細。
# 3. Noisy Nets 學習成本：因為拔除了傳統的 Epsilon-greedy，神經網路必須花大量時間自行調整參數 (mu 與 sigma)，
#    才能收斂出穩定的探索 (Exploration) 與利用 (Exploitation) 平衡。
class RainbowDQN(nn.Module):
    def __init__(self, in_dim=STATE_SIZE, out_dim=4, num_atoms=51, v_min=-10, v_max=10):
        super(RainbowDQN, self).__init__()
        self.num_atoms = num_atoms
        self.out_dim = out_dim
        self.v_min = v_min
        self.v_max = v_max
        self.register_buffer('support', torch.linspace(v_min, v_max, num_atoms))
        
        self.shared = nn.Sequential(
            nn.Linear(in_dim, 512),
            nn.ReLU(),
            nn.Linear(512, 256),
            nn.ReLU()
        )
        
        self.val_hidden = NoisyLinear(256, 256)
        self.val_out = NoisyLinear(256, num_atoms)
        
        self.adv_hidden = NoisyLinear(256, 256)
        self.adv_out = NoisyLinear(256, out_dim * num_atoms)
        
    def forward(self, x):
        features = self.shared(x)
        
        val_feat = F.relu(self.val_hidden(features))
        val = self.val_out(val_feat).view(-1, 1, self.num_atoms)
        
        adv_feat = F.relu(self.adv_hidden(features))
        adv = self.adv_out(adv_feat).view(-1, self.out_dim, self.num_atoms)
        
        q_logits = val + (adv - adv.mean(dim=1, keepdim=True))
        q_dist = F.softmax(q_logits, dim=-1)
        return q_dist

    def get_action(self, x):
        q_dist = self.forward(x)
        q_val = (q_dist * self.support).sum(dim=2)
        return q_val.argmax(dim=1)
        
    def reset_noise(self):
        self.val_hidden.reset_noise()
        self.val_out.reset_noise()
        self.adv_hidden.reset_noise()
        self.adv_out.reset_noise()


class SumTree:
    def __init__(self, capacity):
        self.capacity = capacity
        self.tree = np.zeros(2 * capacity - 1)
        self.data = np.zeros(capacity, dtype=object)
        self.write = 0

    def _propagate(self, idx, change):
        parent = (idx - 1) // 2
        self.tree[parent] += change
        if parent != 0:
            self._propagate(parent, change)

    def _retrieve(self, idx, s):
        left = 2 * idx + 1
        right = left + 1
        if left >= len(self.tree):
            return idx
        if s <= self.tree[left]:
            return self._retrieve(left, s)
        else:
            return self._retrieve(right, s - self.tree[left])

    def total(self):
        return self.tree[0]

    def add(self, p, data):
        idx = self.write + self.capacity - 1
        self.data[self.write] = data
        self.update(idx, p)
        self.write += 1
        if self.write >= self.capacity:
            self.write = 0

    def update(self, idx, p):
        change = p - self.tree[idx]
        self.tree[idx] = p
        self._propagate(idx, change)

    def get(self, s):
        idx = self._retrieve(0, s)
        dataIdx = idx - self.capacity + 1
        return idx, self.tree[idx], self.data[dataIdx]


class PrioritizedReplayBuffer:
    def __init__(self, capacity, alpha=0.6):
        self.tree = SumTree(capacity)
        self.capacity = capacity
        self.alpha = alpha
        self.max_priority = 1.0
        self.size = 0

    def append(self, transition):
        # 保證新存入的經驗被賦予目前的最大優先級 (Max Priority)，強迫模型至少學習一次新經驗
        self.tree.add(self.max_priority ** self.alpha, transition)
        if self.size < self.capacity:
            self.size += 1

    def sample(self, batch_size, beta=0.4):
        batch = []
        indices = []
        priorities = []
        segment = self.tree.total() / batch_size

        for i in range(batch_size):
            a = segment * i
            b = segment * (i + 1)
            s = random.uniform(a, b)
            idx, p, data = self.tree.get(s)
            
            if data == 0 or data is None: 
                continue
                
            batch.append(data)
            indices.append(idx)
            priorities.append(p)

        if len(batch) == 0:
            return None

        sampling_probabilities = np.array(priorities) / self.tree.total()
        is_weights = np.power(self.tree.capacity * sampling_probabilities, -beta)
        is_weights /= is_weights.max()

        states, actions, rewards, next_states, dones = zip(*batch)
        return (
            torch.stack(states),
            torch.tensor(actions, dtype=torch.int64),
            torch.tensor(rewards, dtype=torch.float32),
            torch.stack(next_states),
            torch.tensor(dones, dtype=torch.float32),
            indices,
            torch.tensor(is_weights, dtype=torch.float32)
        )

    def update_priorities(self, indices, errors, offset=1e-5):
        for idx, error in zip(indices, errors):
            p = (error + offset) ** self.alpha
            self.tree.update(idx, p)
            self.max_priority = max(self.max_priority, p)
            
    def __len__(self):
        return self.size


class RLDataset(IterableDataset):
    def __init__(self, buffer, agent, sample_size=128):
        self.buffer = buffer
        self.agent = agent
        self.sample_size = sample_size

    def __iter__(self):
        while True:
            if len(self.buffer) >= self.sample_size:
                yield self.buffer.sample(self.sample_size, beta=self.agent.get_beta())


class RainbowLightning(pl.LightningModule):
    def __init__(self, batch_size=BATCH_SIZE, lr=1e-4, gamma=0.9, sync_rate=0.005, replay_size=50000, n_step=N_STEP):
        super().__init__()
        self.batch_size = batch_size
        self.lr = lr
        self.gamma = gamma
        self.sync_rate = sync_rate 
        self.replay_size = replay_size
        self.n_step = n_step
        
        self.num_atoms = 51
        self.v_min = -20
        self.v_max = 20
        self.dz = (self.v_max - self.v_min) / (self.num_atoms - 1)
        
        self.net = RainbowDQN(num_atoms=self.num_atoms, v_min=self.v_min, v_max=self.v_max)
        self.target_net = copy.deepcopy(self.net)
        for p in self.target_net.parameters():
            p.requires_grad = False
            
        self.buffer = PrioritizedReplayBuffer(self.replay_size)
        self.env = None
        self.state = None
        self.mov = 0
        
        self.n_step_buffer = deque(maxlen=self.n_step)
        self.collected_losses = []
        
        self.populate_buffer(self.batch_size * 2)
        
    def get_beta(self):
        start_beta = 0.4
        end_beta = 1.0
        step = self.global_step
        beta = start_beta + step * (end_beta - start_beta) / MAX_STEPS
        return min(end_beta, beta)

    def get_state(self):
        state_ = self.env.board.render_np().reshape(1, STATE_SIZE) + np.random.rand(1, STATE_SIZE) / 100.0
        return torch.from_numpy(state_).float().squeeze(0)
        
    def reset_env(self):
        self.env = Gridworld(size=GRID_SIZE, mode='random')
        self.state = self.get_state()
        self.mov = 0
        self.n_step_buffer.clear()

    def play_step(self):
        if self.env is None or self.state is None:
            self.reset_env()
            
        with torch.no_grad():
            action = self.net.get_action(self.state.unsqueeze(0).to(self.device)).item()
                
        action_str = action_set[action]
        self.env.makeMove(action_str)
        
        next_state = self.get_state()
        reward = self.env.reward()
        self.mov += 1
        
        done = True if reward > 0 else False
        if reward != -1 or self.mov > 50:
            done = True
            
        self.n_step_buffer.append((self.state, action, reward, next_state, done))
        
        if len(self.n_step_buffer) == self.n_step:
            R = sum([self.gamma**i * exp[2] for i, exp in enumerate(self.n_step_buffer)])
            s_0, a_0, _, _, _ = self.n_step_buffer[0]
            _, _, _, s_n, d_n = self.n_step_buffer[-1]
            self.buffer.append((s_0, a_0, R, s_n, d_n))
            
        if done:
            # 確保當回合結束 (done=True) 時，將 n_step_buffer 中剩餘的經驗依序計算折扣後存入 PER，不可遺漏
            while len(self.n_step_buffer) > 0:
                R = sum([self.gamma**i * exp[2] for i, exp in enumerate(self.n_step_buffer)])
                s_0, a_0, _, _, _ = self.n_step_buffer[0]
                _, _, _, s_n, d_n = self.n_step_buffer[-1]
                self.buffer.append((s_0, a_0, R, s_n, d_n))
                self.n_step_buffer.popleft()
            self.state = None
        else:
            self.state = next_state

    def populate_buffer(self, steps):
        for _ in range(steps):
            self.play_step()

    def soft_update(self):
        for target_param, param in zip(self.target_net.parameters(), self.net.parameters()):
            target_param.data.copy_(self.sync_rate * param.data + (1.0 - self.sync_rate) * target_param.data)

    def forward(self, x):
        return self.net(x)

    def training_step(self, batch, batch_idx):
        self.net.reset_noise()
        self.target_net.reset_noise()
        
        self.play_step()
        
        if batch is None:
            return None
            
        states, actions, rewards, next_states, dones, indices, weights = batch
        bsz = states.size(0)
        
        # 1. Prediction Dist
        dist = self.net(states) # (batch, num_actions, N_atoms)
        action_dist = dist[range(bsz), actions] # (batch, N_atoms)
        
        # 2. C51 Target Projection with Double DQN
        with torch.no_grad():
            next_dist_main = self.net(next_states)
            next_actions = (next_dist_main * self.net.support).sum(2).argmax(1)
            
            next_dist_target = self.target_net(next_states)
            next_action_dist_target = next_dist_target[range(bsz), next_actions]
            
            Tz = rewards.unsqueeze(1) + (self.gamma ** self.n_step) * self.net.support.unsqueeze(0) * (1 - dones.unsqueeze(1))
            Tz = Tz.clamp(self.v_min, self.v_max)
            
            b = (Tz - self.v_min) / self.dz
            l = b.floor().long()
            u = b.ceil().long()
            
            eq_mask = (l == u)
            u[eq_mask & (u < self.num_atoms - 1)] += 1
            l[eq_mask & (l == self.num_atoms - 1)] -= 1
            
            m = torch.zeros_like(next_action_dist_target)
            offset = torch.linspace(0, (bsz - 1) * self.num_atoms, bsz).long().unsqueeze(1).to(self.device)
            
            # 確保 target 分佈 m 的機率總和嚴格等於 1 (由原始機率分佈依照距離比例線性分攤)
            m.view(-1).index_add_(0, (l + offset).view(-1), (next_action_dist_target * (u.float() - b)).view(-1))
            m.view(-1).index_add_(0, (u + offset).view(-1), (next_action_dist_target * (b - l.float())).view(-1))
            
        # 3. Cross Entropy Loss with IS Weights
        loss = - (m * action_dist.clamp(min=1e-5, max=1-1e-5).log()).sum(1)
        
        # 4. Update PER
        errors = loss.detach().cpu().numpy()
        self.buffer.update_priorities(indices, errors)
        
        loss = (loss * weights).mean()
        
        self.soft_update()
        
        self.log('train_loss', loss, prog_bar=True)
        self.collected_losses.append(loss.item())
        
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.net.parameters(), lr=self.lr)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=100000, gamma=0.9)
        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
        
    def train_dataloader(self):
        dataset = RLDataset(self.buffer, self, self.batch_size)
        return DataLoader(dataset, batch_size=None)


def running_mean(x, N=50):
    c = x.shape[0] - N
    if c <= 0:
        return x
    y = np.zeros(c)
    conv = np.ones(N)
    for i in range(c):
        y[i] = (x[i:i+N] @ conv)/N
    return y


def test_model(model, max_games=1000):
    print(f"\nTesting model for {max_games} games in 'random' mode...")
    model.eval() # Disable NoisyNet noise
    wins = 0
    device = next(model.parameters()).device
    
    for i in range(max_games):
        test_game = Gridworld(size=GRID_SIZE, mode='random')
        state_ = test_game.board.render_np().reshape(1, STATE_SIZE) + np.random.rand(1, STATE_SIZE) / 10.0
        state = torch.from_numpy(state_).float().to(device)
        status = 1
        mov = 0
        
        while status == 1:
            mov += 1
            with torch.no_grad():
                action_ = model.net.get_action(state.unsqueeze(0)).item()
            
            action = action_set[action_]
            test_game.makeMove(action)
            
            state_ = test_game.board.render_np().reshape(1, STATE_SIZE) + np.random.rand(1, STATE_SIZE) / 10.0
            state = torch.from_numpy(state_).float().to(device)
            reward = test_game.reward()
            
            if reward != -1:
                if reward > 0:
                    status = 2
                    wins += 1
                else:
                    status = 0
            if mov > 50:
                break
                
    win_rate = float(wins) / float(max_games)
    print(f"Final Win percentage: {win_rate * 100.0:.2f}%\n")
    return win_rate


if __name__ == '__main__':
    pl.seed_everything(42)
    
    model = RainbowLightning(batch_size=BATCH_SIZE)
    
    trainer = pl.Trainer(
        max_steps=MAX_STEPS,
        gradient_clip_val=1.0,
        enable_progress_bar=True,
        log_every_n_steps=100,
    )
    
    print("--- Starting Training with PyTorch Lightning (Full Rainbow DQN) ---")
    trainer.fit(model)
    
    test_model(model, max_games=1000)
    
    plt.figure(figsize=(10, 6))
    losses = np.array(model.collected_losses)
    smoothed_loss = running_mean(losses, N=200)
    
    plt.plot(smoothed_loss, color='#9467bd', linewidth=2.5, alpha=0.9)
    plt.yscale('log')
    plt.xlabel("Training Steps", fontsize=14, fontweight='bold')
    plt.ylabel("Loss (Log Scale)", fontsize=14, fontweight='bold')
    plt.title(f"HW3-4 Full Rainbow DQN Loss (Final Rescue)\n(Mode: Random, Max Steps: {MAX_STEPS})", fontsize=16, fontweight='bold')
    plt.grid(True, which="major", ls="-", alpha=0.6)
    plt.grid(True, which="minor", ls="--", alpha=0.2)
    plt.tight_layout()
    plt.savefig("hw3_4_full_rainbow_final.png", dpi=300)
    print("Saved hw3_4_full_rainbow_final.png")
