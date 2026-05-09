import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import IterableDataset, DataLoader
import pytorch_lightning as pl
import random
import copy
from collections import deque
from matplotlib import pylab as plt
from Gridworld import Gridworld

GRID_SIZE = 4
STATE_SIZE = 4 * GRID_SIZE * GRID_SIZE
MAX_STEPS = 100000
BATCH_SIZE = 200

action_set = {
    0: 'u',
    1: 'd',
    2: 'l',
    3: 'r',
}

class DuelingDQN(nn.Module):
    def __init__(self, l1=STATE_SIZE, l2=150, l3=100, l4=4):
        super(DuelingDQN, self).__init__()
        self.shared = nn.Sequential(
            nn.Linear(l1, l2),
            nn.ReLU(),
            nn.Linear(l2, l3),
            nn.ReLU()
        )
        self.value_stream = nn.Linear(l3, 1)
        self.advantage_stream = nn.Linear(l3, l4)

    def forward(self, x):
        features = self.shared(x)
        val = self.value_stream(features)
        adv = self.advantage_stream(features)
        # Q(s, a) = V(s) + A(s, a) - mean(A(s, a))
        q_vals = val + (adv - adv.mean(dim=1, keepdim=True))
        return q_vals

class ReplayBuffer:
    def __init__(self, capacity):
        self.buffer = deque(maxlen=capacity)

    def __len__(self):
        return len(self.buffer)

    def append(self, experience):
        self.buffer.append(experience)

    def sample(self, batch_size):
        indices = np.random.choice(len(self.buffer), batch_size, replace=False)
        states, actions, rewards, next_states, dones = zip(*[self.buffer[idx] for idx in indices])
        return (
            torch.stack(states),
            torch.tensor(actions, dtype=torch.int64),
            torch.tensor(rewards, dtype=torch.float32),
            torch.stack(next_states),
            torch.tensor(dones, dtype=torch.float32)
        )

class RLDataset(IterableDataset):
    def __init__(self, buffer, sample_size=200):
        self.buffer = buffer
        self.sample_size = sample_size

    def __iter__(self):
        # 產生無限迴圈，交由 Lightning Trainer 的 max_steps 來控制終止
        while True:
            if len(self.buffer) >= self.sample_size:
                yield self.buffer.sample(self.sample_size)

class DQNLightning(pl.LightningModule):
    def __init__(self, batch_size=200, lr=1e-3, gamma=0.9, sync_rate=0.005, replay_size=1000):
        super().__init__()
        self.batch_size = batch_size
        self.lr = lr
        self.gamma = gamma
        # Bonus: 使用 Soft Update (Polyak Averaging) 穩定目標網路
        self.sync_rate = sync_rate 
        self.replay_size = replay_size
        
        self.net = DuelingDQN()
        self.target_net = copy.deepcopy(self.net)
        # 凍結目標網路的梯度
        for p in self.target_net.parameters():
            p.requires_grad = False
            
        self.buffer = ReplayBuffer(self.replay_size)
        self.env = None
        self.state = None
        self.mov = 0
        
        self.loss_fn = nn.MSELoss()
        self.collected_losses = []
        
        # 預先填充 Replay Buffer
        self.populate_buffer(self.batch_size)
        
    def get_epsilon(self):
        # Bonus: Epsilon Decay，隨著全局步驟減少探索率
        start_eps = 1.0
        end_eps = 0.1
        step = self.global_step
        epsilon = start_eps - step * (start_eps - end_eps) / MAX_STEPS
        return max(end_eps, epsilon)

    def get_state(self):
        state_ = self.env.board.render_np().reshape(1, STATE_SIZE) + np.random.rand(1, STATE_SIZE) / 100.0
        return torch.from_numpy(state_).float().squeeze(0) # 轉成 1D
        
    def reset_env(self):
        self.env = Gridworld(size=GRID_SIZE, mode='random')
        self.state = self.get_state()
        self.mov = 0

    def play_step(self):
        """在環境中執行一步並將經驗存入 Replay Buffer"""
        if self.env is None or self.state is None:
            self.reset_env()
            
        epsilon = self.get_epsilon()
        
        if random.random() < epsilon:
            action = np.random.randint(0, 4)
        else:
            with torch.no_grad():
                q_vals = self.net(self.state.unsqueeze(0).to(self.device))
                action = torch.argmax(q_vals, dim=1).item()
                
        action_str = action_set[action]
        self.env.makeMove(action_str)
        
        next_state = self.get_state()
        reward = self.env.reward()
        self.mov += 1
        
        done = True if reward > 0 else False
        if reward != -1 or self.mov > 50:
            done = True
            
        self.buffer.append((self.state, action, reward, next_state, done))
        
        if done:
            self.state = None
        else:
            self.state = next_state

    def populate_buffer(self, steps):
        for _ in range(steps):
            self.play_step()

    def soft_update(self):
        """Bonus: Soft Update 邏輯"""
        for target_param, param in zip(self.target_net.parameters(), self.net.parameters()):
            target_param.data.copy_(self.sync_rate * param.data + (1.0 - self.sync_rate) * target_param.data)

    def forward(self, x):
        return self.net(x)

    def training_step(self, batch, batch_idx):
        # 1. 前進一步，收集經驗
        self.play_step()
        
        # 2. 獲取資料 (PyTorch Lightning 會自動處理 device)
        states, actions, rewards, next_states, dones = batch
        
        # 3. 雙重 DQN 計算損失 (Double DQN Logic)
        q_vals = self.net(states)
        action_q_vals = q_vals.gather(dim=1, index=actions.unsqueeze(1)).squeeze(-1)
        
        with torch.no_grad():
            next_q_vals_main = self.net(next_states)
            best_next_actions = torch.argmax(next_q_vals_main, dim=1)
            
            next_q_vals_target = self.target_net(next_states)
            target_q_vals = next_q_vals_target.gather(dim=1, index=best_next_actions.unsqueeze(1)).squeeze(-1)
            
        target = rewards + self.gamma * (1 - dones) * target_q_vals
        loss = self.loss_fn(action_q_vals, target)
        
        # 4. Soft Update
        self.soft_update()
        
        self.log('train_loss', loss, prog_bar=True)
        self.collected_losses.append(loss.item())
        
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.net.parameters(), lr=self.lr)
        # Bonus: Learning Rate Scheduling，每 20,000 步讓學習率打 9 折
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=20000, gamma=0.9)
        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
        
    def train_dataloader(self):
        dataset = RLDataset(self.buffer, self.batch_size)
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
    model.eval()
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
                qval = model(state)
            
            action_ = torch.argmax(qval, dim=1).item()
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
    
    model = DQNLightning(batch_size=BATCH_SIZE)
    
    # Bonus: 使用 Gradient Clipping 防止梯度爆炸
    trainer = pl.Trainer(
        max_steps=MAX_STEPS,
        gradient_clip_val=1.0,
        enable_progress_bar=True,
        log_every_n_steps=100,
    )
    
    print("--- Starting Training with PyTorch Lightning ---")
    trainer.fit(model)
    
    # 執行測試並回報
    test_model(model, max_games=1000)
    
    # 繪製 Loss 曲線圖
    plt.figure(figsize=(10, 6))
    losses = np.array(model.collected_losses)
    smoothed_loss = running_mean(losses, N=500)
    
    plt.plot(smoothed_loss, color='#d62728', linewidth=2, alpha=0.9)
    plt.yscale('log')
    plt.xlabel("Training Steps", fontsize=14, fontweight='bold')
    plt.ylabel("Loss (Log Scale)", fontsize=14, fontweight='bold')
    plt.title(f"HW3-3 Dueling DQN Loss (Mode: Random, Max Steps: {MAX_STEPS})", fontsize=16, fontweight='bold')
    plt.grid(True, which="major", ls="-", alpha=0.6)
    plt.grid(True, which="minor", ls="--", alpha=0.2)
    plt.tight_layout()
    plt.savefig("hw3_3_loss.png", dpi=300)
    print("Saved hw3_3_loss.png")
