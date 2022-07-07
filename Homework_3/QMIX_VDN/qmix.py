import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from mix_net import QMIX_Net, VDN_Net
from q_net import Q_network_MLP, Q_network_RNN

class QMIX(object):
    def __init__(self, args):
        self.args = args
        self.N = args.N
        self.action_dim = args.action_dim
        self.obs_dim = args.obs_dim
        self.state_dim = args.state_dim
        self.add_last_action = args.add_last_action
        self.add_agent_id = args.add_agent_id
        self.max_train_steps = args.max_train_steps
        self.lr = args.lr
        self.gamma = args.gamma
        self.use_grad_clip = args.use_grad_clip
        self.batch_size = args.batch_size  # 这里的batch_size代表有多少个episode
        self.target_update_freq = args.target_update_freq
        self.tau = args.tau
        self.use_hard_update = args.use_hard_update
        self.use_rnn = args.use_rnn
        self.algorithm = args.algorithm
        self.use_double_q = args.use_double_q
        self.use_RMS = args.use_RMS
        self.use_lr_decay = args.use_lr_decay

        # Compute the input dimension
        self.input_dim = self.obs_dim
        if self.add_last_action:
            print("------add last action------")
            self.input_dim += self.action_dim
        if self.add_agent_id:
            print("------add agent id------")
            self.input_dim += self.N

        # Setup Q Network
        if self.use_rnn:
            print("------use RNN------")
            self.eval_Q_net = Q_network_RNN(args, self.input_dim)
            self.target_Q_net = Q_network_RNN(args, self.input_dim)
        else:
            print("------use MLP------")
            self.eval_Q_net = Q_network_MLP(args, self.input_dim)
            self.target_Q_net = Q_network_MLP(args, self.input_dim)
        self.target_Q_net.load_state_dict(self.eval_Q_net.state_dict())

        # Setup Mixing Network
        if self.algorithm == "QMIX":
            print("------algorithm: QMIX------")
            self.eval_mix_net = QMIX_Net(args)
            self.target_mix_net = QMIX_Net(args)
        elif self.algorithm == "VDN":
            print("------algorithm: VDN------")
            self.eval_mix_net = VDN_Net()
            self.target_mix_net = VDN_Net()
        else:
            raise NotImplementedError(f"{self.algorithm} Not supported!")
        self.target_mix_net.load_state_dict(self.eval_mix_net.state_dict())

        # Setup optimizer
        self.eval_parameters = list(self.eval_mix_net.parameters()) + list(self.eval_Q_net.parameters())
        if self.use_RMS:
            print("------optimizer: RMSprop------")
            self.optimizer = torch.optim.RMSprop(self.eval_parameters, lr=self.lr)
        else:
            print("------optimizer: Adam------")
            self.optimizer = torch.optim.Adam(self.eval_parameters, lr=self.lr)

        self.train_step = 0

    def choose_action(self, obs_n, last_onehot_a_n, epsilon):
        """Choose an action for each agent based on its local observations.

        Args:
            obs_n: array(N, obs_dim)
                Each agent's local observation.
            last_onehot_a_n: array(N, action_dim)
                Each agent's last taken action, in a one-hot encoding.
            epsilon: float
                Epsilon-greedy argument.

        Returns:
            a_n: array(N, )
                Discrete actions (not one-hot) for each agent.

        """
        with torch.no_grad():
            if np.random.uniform() < epsilon:
                # epsilon-greedy
                # Only available actions can be chosen
                # a_n = [np.random.choice(np.nonzero(avail_a)[0]) for avail_a in avail_a_n]
                a_n = [np.random.choice(self.action_dim) for _ in range(self.N)]
            else:
                inputs = []
                # obs_n.shape=(N，obs_dim)
                obs_n = torch.tensor(obs_n, dtype=torch.float32)

                inputs.append(obs_n)
                if self.add_last_action:
                    last_a_n = torch.tensor(last_onehot_a_n, dtype=torch.float32)
                    inputs.append(last_a_n)
                if self.add_agent_id:
                    inputs.append(torch.eye(self.N))

                # inputs.shape=(N, inputs_dim)
                inputs = torch.cat([x for x in inputs], dim=-1)

                q_value = self.eval_Q_net(inputs)
                # avail_a_n = torch.tensor(avail_a_n, dtype=torch.float32)  # avail_a_n.shape=(N, action_dim)
                # q_value[avail_a_n == 0] = -float('inf')  # Mask the unavailable actions
                a_n = q_value.argmax(dim=-1).numpy()
            return a_n

    def train(self, replay_buffer):
        """QMIX training script.
        
        Args:
            replay_buffer: Where transactions stored.
        """

        self.train_step += 1
        batch, max_episode_len = replay_buffer.sample()
        # inputs.shape=(bach_size, max_episode_len + 1, N, input_dim)
        inputs = self.get_inputs(batch, max_episode_len)

        if self.use_rnn:
            self.eval_Q_net.rnn_hidden = None
            self.target_Q_net.rnn_hidden = None
            q_evals, q_targets = [], []

            for t in range(max_episode_len):  # t=0,1,2,...(episode_len-1)
                # q_eval.shape=(batch_size*N,action_dim)
                q_eval = self.eval_Q_net(inputs[:, t].reshape(-1, self.input_dim))
                q_target = self.target_Q_net(
                    inputs[:, t + 1].reshape(-1, self.input_dim)
                )
                # q_eval.shape=(batch_size,N,action_dim)
                q_evals.append(q_eval.reshape(self.batch_size, self.N, -1))
                q_targets.append(q_target.reshape(self.batch_size, self.N, -1))

            # Stack them according to the time (dim=1)
            # q_evals.shape=(batch_size,max_episode_len,N,action_dim)
            q_evals = torch.stack(q_evals, dim=1)
            q_targets = torch.stack(q_targets, dim=1)
        else:
            # q_evals.shape=(batch_size,max_episode_len,N,action_dim)
            q_evals = self.eval_Q_net(inputs[:, :-1])
            q_targets = self.target_Q_net(inputs[:, 1:])

        with torch.no_grad():
            # If use double q-learning, we use eval_net to choose actions,and use target_net to compute q_target
            if self.use_double_q:
                q_eval_last = self.eval_Q_net(
                    inputs[:, -1].reshape(-1, self.input_dim)
                ).reshape(self.batch_size, 1, self.N, -1)
                # q_evals_next.shape=(batch_size,max_episode_len,N,action_dim)
                q_evals_next = torch.cat([q_evals[:, 1:], q_eval_last], dim=1)
                # q_evals_next[batch['avail_a_n'][:, 1:] == 0] = -999999
                # a_max.shape=(batch_size,max_episode_len, N, 1)
                a_argmax = torch.argmax(q_evals_next, dim=-1, keepdim=True)
                # q_targets.shape=(batch_size, max_episode_len, N)
                q_targets = torch.gather(q_targets, dim=-1, index=a_argmax).squeeze(-1)
            else:
                # q_targets[batch['avail_a_n'][:, 1:] == 0] = -999999
                # q_targets.shape=(batch_size, max_episode_len, N)
                q_targets = q_targets.max(dim=-1)[0]

        # batch['a_n'].shape(batch_size,max_episode_len, N)
        # q_evals.shape(batch_size, max_episode_len, N)
        q_evals = torch.gather(
            q_evals, dim=-1, index=batch['a_n'].unsqueeze(-1)
        ).squeeze(-1)

        # Compute q_total using QMIX or VDN, q_total.shape=(batch_size, max_episode_len, 1)
        if self.algorithm == "QMIX":
            q_total_eval = self.eval_mix_net(q_evals, batch['s'][:, :-1])
            q_total_target = self.target_mix_net(q_targets, batch['s'][:, 1:])
        else:
            q_total_eval = self.eval_mix_net(q_evals)
            q_total_target = self.target_mix_net(q_targets)
        # targets.shape=(batch_size,max_episode_len,1)
        targets = batch['r'] + self.gamma * (1 - batch['dw']) * q_total_target

        td_error = q_total_eval - targets.detach()
        mask_td_error = td_error * batch['active']
        loss = (mask_td_error**2).sum() / batch['active'].sum()

        self.optimizer.zero_grad()
        loss.backward()
        if self.use_grad_clip:
            torch.nn.utils.clip_grad_norm_(self.eval_parameters, 10)
        self.optimizer.step()

        if self.use_hard_update:
            # hard update
            if self.train_step % self.target_update_freq == 0:
                self.target_Q_net.load_state_dict(self.eval_Q_net.state_dict())
                self.target_mix_net.load_state_dict(self.eval_mix_net.state_dict())
        else:
            # Softly update the target networks
            for param, target_param in zip(
                self.eval_Q_net.parameters(), self.target_Q_net.parameters()
            ):
                target_param.data.copy_(
                    self.tau * param.data + (1 - self.tau) * target_param.data
                )

            for param, target_param in zip(
                self.eval_mix_net.parameters(), self.target_mix_net.parameters()
            ):
                target_param.data.copy_(
                    self.tau * param.data + (1 - self.tau) * target_param.data
                )

        if self.use_lr_decay:
            self.lr_decay(self.train_step)

        if self.train_step % self.args.save_freq == 0:
            self.save_model()

    def lr_decay(self, current_training_step):  # Learning rate Decay
        lr_now = self.lr * (1 - current_training_step / self.max_train_steps)
        for p in self.optimizer.param_groups:
            p['lr'] = lr_now

    def get_inputs(self, batch, max_episode_len):
        inputs = []
        inputs.append(batch['obs_n'])
        if self.add_last_action:
            inputs.append(batch['last_onehot_a_n'])
        if self.add_agent_id:
            agent_id_one_hot = (
                torch.eye(self.N)
                .unsqueeze(0)
                .unsqueeze(0)
                .repeat(self.batch_size, max_episode_len + 1, 1, 1)
            )
            inputs.append(agent_id_one_hot)

        # inputs.shape=(bach_size, max_episode_len + 1, N, input_dim)
        inputs = torch.cat([x for x in inputs], dim=-1)
        return inputs

    def save_model(self):
        torch.save(
            self.eval_Q_net.state_dict(),
            "./model/{}_eval_rnn_{}_seed_{}_step_{}k.pth".format(self.algorithm, self.args.save_id, self.args.seed, int(self.train_step / 1000)
            ),
        )
