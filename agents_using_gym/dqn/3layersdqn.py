#!/usr/bin/env python
import os,sys
sys.path.insert(1, os.path.join(sys.path[0], '..'))
import argparse

from multiagent.environment import MultiAgentEnv
import multiagent.scenarios as scenarios
import numpy as np
import keras.backend.tensorflow_backend as backend
from keras.models import Sequential
from keras.layers import Dense, Dropout, Conv2D, MaxPooling2D, Activation, Flatten
from keras.optimizers import Adam
from keras.callbacks import TensorBoard
import tensorflow as tf
from collections import deque
import time
import random
from tqdm import tqdm
from PIL import Image



if __name__ == '__main__':
    # parse arguments
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('-s', '--scenario', default='simple.py', help='Path of the scenario Python script.')
    args = parser.parse_args()

    # load scenario from script
    scenario = scenarios.load(args.scenario).Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, info_callback=None, shared_viewer = False)
    # render call to create viewer window (necessary only for interactive policies)
    env.render()
    
    # execution loop
    obs_n = env.reset()
    
    DISCOUNT = 0.99
    REPLAY_MEMORY_SIZE = 200  # How many last steps to keep for model training
    MIN_REPLAY_MEMORY_SIZE =100   # Minimum number of steps in a memory to start training
    MINIBATCH_SIZE = 64  # How many steps (samples) to use for training
    UPDATE_TARGET_EVERY = 10  # Terminal states (end of episodes)
    MODEL_NAME = '32'
    MIN_REWARD = 20  # For model save
    MEMORY_FRACTION = 0.20
    
    # Environment settings
    EPISODES = 2000
    
    # Exploration settings
    epsilon = 1  # not a constant, going to be decayed
    EPSILON_DECAY = 0.99975
    MIN_EPSILON = 0.001
    
    #  Stats settings
    AGGREGATE_STATS_EVERY = 50  # episodes
    SHOW_PREVIEW = False    
    
    
    # For stats
    ep_rewards = [[-200]]*len(obs_n)
    
    # For more repetitive results
    random.seed(1)
    np.random.seed(1)
    tf.set_random_seed(1)
    
    # Memory fraction, used mostly when trai8ning multiple agents
    #gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=MEMORY_FRACTION)
    #backend.set_session(tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)))
    
    # Create models folder
    if not os.path.isdir('models'):
        os.makedirs('models')
    
    
    # Own Tensorboard class
    class ModifiedTensorBoard(TensorBoard):
    
        # Overriding init to set initial step and writer (we want one log file for all .fit() calls)
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.step = 1
            self.writer = tf.summary.FileWriter(self.log_dir)
    
        # Overriding this method to stop creating default log writer
        def set_model(self, model):
            pass
    
        # Overrided, saves logs with our step number
        # (otherwise every .fit() will start writing from 0th step)
        def on_epoch_end(self, epoch, logs=None):
            self.update_stats(**logs)
    
        # Overrided
        # We train for one batch only, no need to save anything at epoch end
        def on_batch_end(self, batch, logs=None):
            pass
    
        # Overrided, so won't close writer
        def on_train_end(self, _):
            pass
    
        # Custom method for saving own metrics
        # Creates writer, writes custom metrics and closes writer
        def update_stats(self, **stats):
            self._write_logs(stats, self.step)
    
    
    # Agent class
    class DQNAgent:
        def __init__(self,i):
            self.index=i
            # Main model
            self.model = self.create_model()
    
            # Target network
            self.target_model = self.create_model()
            self.target_model.set_weights(self.model.get_weights())
    
            # An array with last n steps for training
            self.replay_memory = deque(maxlen=REPLAY_MEMORY_SIZE)
    
            # Custom tensorboard object
            self.tensorboard = ModifiedTensorBoard(log_dir="logs/{}-{}-{}".format(MODEL_NAME, self.index,int(time.time())))
    
            # Used to count when to update target network with main network's weights
            self.target_update_counter = 0
    
        def create_model(self):
            model = Sequential()
            model.add(Dense(len(obs_n[0])))
            #model.add(Conv2D(256, (3, 3), input_shape=(10, 10, 3)))  # OBSERVATION_SPACE_VALUES = (10, 10, 3) a 10x10 RGB image.
            model.add(Activation('relu'))
            #model.add(MaxPooling2D(pool_size=(2, 2)))
            
            #model.add(Dropout(0.2))
    
            #model.add(Conv2D(256, (3, 3)))
            #model.add(Activation('relu'))
            #model.add(MaxPooling2D(pool_size=(2, 2)))
            #model.add(Dropout(0.2))
    
           # model.add(Flatten())  # this converts our 3D feature maps to 1D feature vectors
            model.add(Dense(32,activation='relu'))
    
            model.add(Dense(5, activation='linear'))  # ACTION_SPACE_SIZE = how many choices (9)
            model.compile(loss="mse", optimizer=Adam(lr=0.001), metrics=['accuracy'])
            return model
    
        # Adds step's data to a memory replay array
        # (observation space, action, reward, new observation space, done)
        def update_replay_memory(self, transition):
            self.replay_memory.append(transition)
    
        # Trains main network every step during episode
        def train(self, terminal_state, step):
    
            # Start training only if certain number of samples is already saved
            if len(self.replay_memory) < MIN_REPLAY_MEMORY_SIZE:
                return
    
            # Get a minibatch of random samples from memory replay table
            minibatch = random.sample(self.replay_memory, MINIBATCH_SIZE)
    
            # Get current states from minibatch, then query NN model for Q values
            current_states = (np.array([transition[0] for transition in minibatch])+1)/2
            current_qs_list = self.model.predict(current_states)
    
            # Get future states from minibatch, then query NN model for Q values
            # When using target network, query it, otherwise main network should be queried
            new_current_states = (np.array([transition[3] for transition in minibatch])+1)/2
            future_qs_list = self.target_model.predict(new_current_states)
    
            X = []
            y = []
    
            # Now we need to enumerate our batches
            for index, (current_state, action, reward, new_current_state, done) in enumerate(minibatch):
    
                # If not a terminal state, get new q from future states, otherwise set it to 0
                # almost like with Q Learning, but we use just part of equation here
                if not done:
                    max_future_q = np.max(future_qs_list[index])
                    new_q = reward + DISCOUNT * max_future_q
                else:
                    new_q = reward
    
                # Update Q value for given state
                current_qs = current_qs_list[index]
                current_qs[action] = new_q
    
                # And append to our training data
                X.append(current_state)
                y.append(current_qs)
    
            # Fit on all samples as one batch, log only on terminal state
            self.model.fit((np.array(X)+1)/2, np.array(y), batch_size=MINIBATCH_SIZE, verbose=0, shuffle=False, callbacks=[self.tensorboard] if terminal_state else None)
    
            # Update target network counter every episode
            if terminal_state:
                self.target_update_counter += 1
    
            # If counter reaches set value, update target network with weights of main network
            if self.target_update_counter > UPDATE_TARGET_EVERY:
                self.target_model.set_weights(self.model.get_weights())
                self.target_update_counter = 0
    
        # Queries main network for Q values given current observation space (environment state)
        def get_qs(self, state):
            
            return self.model.predict((np.array(state).reshape(-1, *state.shape)+1)/2)[0]
    
    
    
    
    
    
    
    
    
    
    
    
    # create interactive policies for each agent
    policies = [DQNAgent(i) for i in range(env.n)]
    
    for episode in tqdm(range(1, EPISODES + 1), ascii=True, unit='episodes'):
        episode_reward=[0,0,0]
        step=1
        for i, policy in enumerate(policies):
            policy.tensorboard.step=episode
        # query for action from each agent's policy
        obs_n=env.reset()
        done = False
        while not done:
            
            act_n = []
            action_n=[]
            for i, policy in enumerate(policies):
                act = np.zeros(5)
                if np.random.random() > epsilon:
                    # Get action from Q table
                    action = np.argmax(policy.get_qs(obs_n[i]))
                else:
                    # Get random action
                    action = np.random.randint(0, 5)                
                act[action]+=1.0
                action_n.append(action)
                act_n.append(act)
                # step environment
            newobs_n, reward_n, done_n, _ = env.step(act_n)
            if step>=100:
                done=True
            for i, policy in enumerate(policies):
                episode_reward[i]+=reward_n[i]
                policy.update_replay_memory((obs_n[i], action_n[i], reward_n[i], newobs_n[i], done))
                policy.train(done, step)                
                
                obs_n=newobs_n
            step+=1
            #if SHOW_PREVIEW and not episode % AGGREGATE_STATS_EVERY:
            if episode % 100==1:
                env.render()
        for i, policy in enumerate(policies):
            ep_rewards[i].append(episode_reward[i])
            if not episode % AGGREGATE_STATS_EVERY or episode == 1:
                average_reward = sum(ep_rewards[i][-AGGREGATE_STATS_EVERY:])/len(ep_rewards[i][-AGGREGATE_STATS_EVERY:])
                min_reward = min(ep_rewards[i][-AGGREGATE_STATS_EVERY:])
                max_reward = max(ep_rewards[i][-AGGREGATE_STATS_EVERY:])
                policy.tensorboard.update_stats(reward_avg=average_reward, reward_min=min_reward, reward_max=max_reward, epsilon=epsilon)
        
                # Save model, but only when min reward is greater or equal a set value
                if min_reward >= MIN_REWARD:
                    policy.model.save(f'models/{MODEL_NAME+str(policy.index)}__{max_reward:_>7.2f}max_{average_reward:_>7.2f}avg_{min_reward:_>7.2f}min__{int(time.time())}.model')            

        if epsilon > MIN_EPSILON:
            epsilon *= EPSILON_DECAY
            epsilon = max(MIN_EPSILON, epsilon)
        
