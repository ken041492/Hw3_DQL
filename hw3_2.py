import numpy as np
import torch
import torch.nn as nn
import random
import copy
from collections import deque
from matplotlib import pylab as plt
from Gridworld import Gridworld

GRID_SIZE = 8
STATE_SIZE = 4 * GRID_SIZE * GRID_SIZE

action_set = {
    0: 'u',
    1: 'd',
    2: 'l',
    3: 'r',
}

class BasicDQN(nn.Module):
    def __init__(self, l1=STATE_SIZE, l2=150, l3=100, l4=4):
        super(BasicDQN, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(l1, l2),
            nn.ReLU(),
            nn.Linear(l2, l3),
            nn.ReLU(),
            nn.Linear(l3, l4)
        )

    def forward(self, x):
        return self.model(x)

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
        # Q(s, a) = V(s) + (A(s, a) - mean(A(s, a)))
        q_vals = val + (adv - adv.mean(dim=1, keepdim=True))
        return q_vals

def train_agent(agent_type='Basic DQN', mode='player', epochs=5000, batch_size=200, mem_size=1000, sync_freq=500):
    print(f"\n--- Starting Training for {agent_type} ---")
    if agent_type == 'Dueling DQN':
        model = DuelingDQN()
    else:
        model = BasicDQN()
        
    model2 = copy.deepcopy(model)
    model2.load_state_dict(model.state_dict())
    
    loss_fn = torch.nn.MSELoss()
    learning_rate = 1e-3
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    
    gamma = 0.9
    epsilon_start = 1.0
    epsilon_end = 0.1
    epsilon = epsilon_start
    epsilon_decay = (epsilon_start - epsilon_end) / epochs
    
    losses = []
    replay = deque(maxlen=mem_size)
    max_moves = 50
    j = 0
    
    for i in range(epochs):
        game = Gridworld(size=GRID_SIZE, mode=mode)
        state1_ = game.board.render_np().reshape(1,STATE_SIZE) + np.random.rand(1,STATE_SIZE)/100.0
        state1 = torch.from_numpy(state1_).float()
        status = 1
        mov = 0
        
        while(status == 1):
            j += 1
            mov += 1
            qval = model(state1)
            qval_ = qval.data.numpy()
            
            if random.random() < epsilon:
                action_ = np.random.randint(0,4)
            else:
                action_ = np.argmax(qval_)
                
            action = action_set[action_]
            game.makeMove(action)
            state2_ = game.board.render_np().reshape(1,STATE_SIZE) + np.random.rand(1,STATE_SIZE)/100.0
            state2 = torch.from_numpy(state2_).float()
            reward = game.reward()
            done = True if reward > 0 else False
            exp = (state1, action_, reward, state2, done)
            replay.append(exp)
            state1 = state2
            
            if len(replay) > batch_size:
                minibatch = random.sample(replay, batch_size)
                state1_batch = torch.cat([s1 for (s1,a,r,s2,d) in minibatch])
                action_batch = torch.Tensor([a for (s1,a,r,s2,d) in minibatch])
                reward_batch = torch.Tensor([r for (s1,a,r,s2,d) in minibatch])
                state2_batch = torch.cat([s2 for (s1,a,r,s2,d) in minibatch])
                done_batch = torch.Tensor([d for (s1,a,r,s2,d) in minibatch])
                
                Q1 = model(state1_batch)
                
                with torch.no_grad():
                    if agent_type == 'Double DQN':
                        # Double DQN Logic
                        Q1_next = model(state2_batch)
                        best_actions = torch.argmax(Q1_next, dim=1)
                        Q2_next = model2(state2_batch)
                        target_Q = Q2_next.gather(dim=1, index=best_actions.unsqueeze(1)).squeeze()
                    else: 
                        # Basic DQN & Dueling DQN Logic
                        Q2_next = model2(state2_batch)
                        target_Q = torch.max(Q2_next, dim=1)[0]
                
                Y = reward_batch + gamma * ((1 - done_batch) * target_Q)
                X = Q1.gather(dim=1, index=action_batch.long().unsqueeze(1)).squeeze()
                
                loss = loss_fn(X, Y.detach())
                optimizer.zero_grad()
                loss.backward()
                losses.append(loss.item())
                optimizer.step()
                
                if j % sync_freq == 0:
                    model2.load_state_dict(model.state_dict())
            
            if reward != -1 or mov > max_moves:
                status = 0
                mov = 0
                
        if epsilon > epsilon_end:
            epsilon -= epsilon_decay
            
        if (i+1) % 500 == 0:
            avg_loss = np.mean(losses[-500:]) if len(losses) > 0 else 0
            print(f"Epoch {i+1:4d}/{epochs} - Avg Loss (last 500): {avg_loss:.4f} - Epsilon: {epsilon:.2f}")

    return model, losses

def test_model(model, mode='player', max_games=1000):
    print(f"Testing model for {max_games} games...")
    wins = 0
    for i in range(max_games):
        test_game = Gridworld(size=GRID_SIZE, mode=mode)
        state_ = test_game.board.render_np().reshape(1,STATE_SIZE) + np.random.rand(1,STATE_SIZE)/10.0
        state = torch.from_numpy(state_).float()
        status = 1
        mov = 0
        while status == 1:
            mov += 1
            qval = model(state)
            qval_ = qval.data.numpy()
            action_ = np.argmax(qval_)
            action = action_set[action_]
            test_game.makeMove(action)
            state_ = test_game.board.render_np().reshape(1,STATE_SIZE) + np.random.rand(1,STATE_SIZE)/10.0
            state = torch.from_numpy(state_).float()
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
    print(f"Win percentage: {win_rate*100.0:.2f}%\n")
    return win_rate

def running_mean(x, N=50):
    c = x.shape[0] - N
    if c <= 0:
        return x
    y = np.zeros(c)
    conv = np.ones(N)
    for i in range(c):
        y[i] = (x[i:i+N] @ conv)/N
    return y

if __name__ == '__main__':
    agent_types = ['Basic DQN', 'Double DQN', 'Dueling DQN']
    mode = 'player'
    epochs = 1500
    
    all_losses = {}
    win_rates = {}
    
    for agent in agent_types:
        model, losses = train_agent(agent_type=agent, mode=mode, epochs=epochs)
        all_losses[agent] = losses
        win_rate = test_model(model, mode=mode, max_games=1000)
        win_rates[agent] = win_rate
        
    print("=" * 40)
    print("Final Comparison:")
    for agent, wr in win_rates.items():
        print(f"{agent}: Win Rate = {wr*100:.2f}%")
        
    # Plotting Loss Curves
    plt.figure(figsize=(12, 8))
    
    # 定義高質感的顏色對應
    colors = {'Basic DQN': '#1f77b4', 'Double DQN': '#ff7f0e', 'Dueling DQN': '#2ca02c'}
    
    for agent in agent_types:
        # 使用 N=200 來大幅平滑化，展現出大氣、穩定的趨勢線
        smoothed_loss = running_mean(np.array(all_losses[agent]), N=200)
        plt.plot(smoothed_loss, label=agent, color=colors[agent], linewidth=2.5, alpha=0.85)
    
    plt.yscale('log')  # 使用對數座標，大突波與小細節同時完美呈現
    
    # 設定標題與軸標籤的字體大小與粗細
    plt.xlabel("Training Steps (Minibatches)", fontsize=16, fontweight='bold')
    plt.ylabel("Loss (Log Scale)", fontsize=16, fontweight='bold')
    plt.title(f"DQN Variants Loss Comparison (Grid Size: {GRID_SIZE}x{GRID_SIZE})", fontsize=20, fontweight='bold')
    
    # 設定精緻的格線與圖例
    plt.grid(True, which="major", ls="-", alpha=0.6)   # 主格線
    plt.grid(True, which="minor", ls="--", alpha=0.2)  # 次格線
    plt.legend(fontsize=14, loc='upper right', frameon=True, shadow=True, borderpad=1)
    
    # 自動排版並存成高解析度 (300 dpi) 圖片
    plt.tight_layout()
    plt.savefig("hw3_2_loss.png", dpi=300)
    print("Saved hw3_2_loss.png")
    
    # Plotting Win Rate Bar Chart
    plt.figure(figsize=(8, 6))
    bars = plt.bar(win_rates.keys(), [wr * 100 for wr in win_rates.values()], color=['blue', 'orange', 'green'])
    plt.ylabel("Win Rate (%)", fontsize=14)
    plt.title("Win Rate Comparison", fontsize=16)
    plt.ylim(0, 105)
    for bar in bars:
        yval = bar.get_height()
        plt.text(bar.get_x() + bar.get_width()/2, yval + 1, f'{yval:.1f}%', ha='center', va='bottom', fontsize=12)
    plt.savefig("win_rate_comparison.png")
    print("Saved win_rate_comparison.png")
