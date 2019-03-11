from collections import deque
from os import path, mkdir
import threading
import time
import math
import numpy as np
import pickle
import concurrent.futures

from neural_network import NeuralNetWorkWrapper
from gomoku_gui import GomokuGUI

import sys
sys.path.append('../build')
from library import MCTS, Gomoku, NeuralNetwork

def tuple_2d_to_numpy_2d(tuple_2d):
    # help function
    # convert type
    res = [None] * len(tuple_2d)
    for i, tuple_1d in enumerate(tuple_2d):
        res[i] = list(tuple_1d)
    return np.array(res)


class Leaner():
    def __init__(self, config):
        # see config.py
        # gomoku
        self.n = config['n']
        self.n_in_row = config['n_in_row']
        self.gomoku_gui = GomokuGUI(config['n'], config['human_color'])
        self.action_size = self.n ** 2

        # train
        self.num_iters = config['num_iters']
        self.num_eps = config['num_eps']
        self.parallel_play_size = config['parallel_play_size']
        self.check_freq = config['check_freq']
        self.contest_num = config['contest_num']
        self.dirichlet_alpha = config['dirichlet_alpha']
        self.temp = config['temp']
        self.update_threshold = config['update_threshold']
        self.explore_num = config['explore_num']

        self.examples_buffer = deque([], maxlen=config['examples_buffer_max_len'])

        # mcts
        self.num_mcts_sims = config['num_mcts_sims']
        self.c_puct = config['c_puct']
        self.c_virtual_loss = config['c_virtual_loss']
        self.thread_pool_size = config['thread_pool_size']
        self.libtorch_use_gpu = config['libtorch_use_gpu']

        # neural network
        self.batch_size = config['batch_size']
        self.train_use_gpu = config['train_use_gpu']

        self.nnet = NeuralNetWorkWrapper(config['lr'], config['l2'], config['epochs'],
                                         config['num_channels'], config['n'], self.action_size, self.train_use_gpu, self.libtorch_use_gpu)

    def learn(self):
        # train the model by self play
        t = threading.Thread(target=self.gomoku_gui.loop)
        t.start()

        if path.exists(path.join('models', 'checkpoint.example')):
            print("loading checkpoint...")
            self.nnet.load_model()
            self.load_samples()

        # save torchscript
        self.nnet.save_model()
        self.nnet.save_model('models', "best_checkpoint")

        for i in range(1, self.num_iters + 1):
            print("ITER :: " + str(i))

            # self play in parallel
            libtorch = NeuralNetwork('./models/checkpoint.pt',
                                     self.libtorch_use_gpu, self.thread_pool_size * self.parallel_play_size)

            with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel_play_size) as executor:
                futures = [executor.submit(self.self_play, 1 if k % 2 else -1, libtorch, k == 1) for k in range(1, self.num_eps + 1)]
                for k, f in enumerate(futures):
                    examples = f.result()
                    print("EPS: {}, STEPS: {}".format(k, len(examples) // 8))
                    self.examples_buffer.extend(examples)

            # need more samples
            if len(self.examples_buffer) < self.batch_size:
                continue

            # train neural network
            self.nnet.train(self.examples_buffer, self.batch_size)
            self.nnet.save_model()
            self.save_samples()

            # compare performance
            if i % self.check_freq == 0:
                libtorch = NeuralNetwork('./models/checkpoint.pt',
                                         self.libtorch_use_gpu, self.thread_pool_size)
                mcts = MCTS(libtorch, self.thread_pool_size, self.c_puct,
                            self.num_mcts_sims, self.c_virtual_loss, self.action_size)
                libtorch_best = NeuralNetwork('./models/best_checkpoint.pt',
                                              self.libtorch_use_gpu, self.thread_pool_size)
                mcts_best = MCTS(libtorch_best, self.thread_pool_size, self.c_puct,
                                 self.num_mcts_sims, self.c_virtual_loss, self.action_size)

                one_won, two_won, draws = self.contest(mcts, mcts_best, self.contest_num)
                print("NEW/PREV WINS : %d / %d ; DRAWS : %d" % (one_won, two_won, draws))

                if one_won + two_won > 0 and float(one_won) / (one_won + two_won) > self.update_threshold:
                    print('ACCEPTING NEW MODEL')
                    self.nnet.save_model('models', "best_checkpoint")
                else:
                    print('REJECTING NEW MODEL')

        t.join()

    def self_play(self, first_color, libtorch, show):
        """
        This function executes one episode of self-play, starting with player 1.
        As the game is played, each turn is added as a training example to
        train_examples. The game is played till the game ends. After the game
        ends, the outcome of the game is used to assign values to each example
        in train_examples.
        """
        train_examples = []
        gomoku = Gomoku(self.n, self.n_in_row, first_color)
        mcts = MCTS(libtorch, self.thread_pool_size, self.c_puct,
                    self.num_mcts_sims, self.c_virtual_loss, self.action_size)

        if show:
            self.gomoku_gui.reset_status()

        episode_step = 0
        while True:
            episode_step += 1

            # prob
            temp = self.temp if episode_step <= self.explore_num else 0
            prob = np.array(list(mcts.get_action_probs(gomoku, temp)))

            # dirichlet noise
            legal_moves = list(gomoku.get_legal_moves())
            noise = 0.25 * np.random.dirichlet(self.dirichlet_alpha * np.ones(np.count_nonzero(legal_moves)))

            prob_noise = 0.75 * prob
            j = 0
            for i in range(len(prob_noise)):
                if legal_moves[i] == 1:
                    prob_noise[i] += noise[j]
                    j += 1
            prob_noise /= np.sum(prob_noise)

            # generate sample
            board = tuple_2d_to_numpy_2d(gomoku.get_board())
            last_action = gomoku.get_last_move()
            cur_player = gomoku.get_current_color()

            sym = self.get_symmetries(board, prob_noise)
            for b, p in sym:
                train_examples.append([b, last_action, cur_player, p])

            # execute move
            action = np.random.choice(len(prob_noise), p=prob_noise)

            if show:
                self.gomoku_gui.execute_move(cur_player, action)
            gomoku.execute_move(action)
            mcts.update_with_move(action)

            # is ended
            ended, winner = gomoku.get_game_status()
            if ended == 1:
                # b, last_action, cur_player, p, v
                return [(x[0], x[1], x[2], x[3], x[2] * winner) for x in train_examples]

    def contest(self, player1, player2, contest_num):
        """compare new and old model
           Args: player1, player2 is white/balck player
           Return: one_won, two_won, draws
        """

        one_won, two_won, draws = 0, 0, 0

        for i in range(contest_num):
            if i < contest_num // 2:
                # first half, player1 is white
                winner = self._contest(player1, player2, 1)
                if winner == 1:
                    one_won += 1
                elif winner == -1:
                    two_won += 1
                else:
                    draws += 1
            else:
                # second half, player1 is black
                winner = self._contest(player2, player1, 1)
                if winner == 1:
                    two_won += 1
                elif winner == -1:
                    one_won += 1
                else:
                    draws += 1

        return one_won, two_won, draws

    def _contest(self, player1, player2, first_player):
        # old model play with new model

        players = [player2, None, player1]
        player_index = first_player
        gomoku = Gomoku(self.n, self.n_in_row, first_player)
        self.gomoku_gui.reset_status()

        while True:
            player = players[player_index + 1]

            # select best move
            prob = player.get_action_probs(gomoku)
            best_move = int(np.argmax(np.array(list(prob))))

            # execute move
            gomoku.execute_move(best_move)
            self.gomoku_gui.execute_move(player_index, best_move)

            # check game status
            ended, winner = gomoku.get_game_status()
            if ended == 1:
                return winner

            # update search tree
            player1.update_with_move(best_move)
            player2.update_with_move(best_move)

            # next player
            player_index = -player_index

    def get_symmetries(self, board, pi):
        # mirror, rotational
        assert(len(pi) == self.action_size)  # 1 for pass

        pi_board = np.reshape(pi, (self.n, self.n))
        l = []

        for i in range(1, 5):
            for j in [True, False]:
                newB = np.rot90(board, i)
                newPi = np.rot90(pi_board, i)
                if j:
                    newB = np.fliplr(newB)
                    newPi = np.fliplr(newPi)
                l += [(newB, newPi.ravel())]
        return l

    def play_with_human(self, human_first=True, checkpoint_name="best_checkpoint"):
        t = threading.Thread(target=self.gomoku_gui.loop)
        t.start()

        # load best model
        libtorch_best = NeuralNetwork('./models/best_checkpoint.pt',
                                        self.libtorch_use_gpu, self.thread_pool_size * 2)
        mcts_best = MCTS(libtorch_best, self.thread_pool_size * 2, self.c_puct,
                            self.num_mcts_sims * 4, self.c_virtual_loss, self.action_size)

        # create gomoku game
        human_color = self.gomoku_gui.get_human_color()
        gomoku = Gomoku(self.n, self.n_in_row, human_color if human_first else -human_color)

        players = ["alpha", None, "human"] if human_color == 1 else ["human", None, "alpha"]
        player_index = human_color if human_first else -human_color

        while True:
            player = players[player_index + 1]

            # select move
            if player == "alpha":
                prob = mcts_best.get_action_probs(gomoku)
                best_move = int(np.argmax(np.array(list(prob))))
                self.gomoku_gui.execute_move(player_index, best_move)
            else:
                self.gomoku_gui.set_is_human(True)
                # wait human action
                while self.gomoku_gui.get_is_human():
                    time.sleep(0.1)
                best_move = self.gomoku_gui.get_human_move()

            # execute move
            gomoku.execute_move(best_move)

            # check game status
            ended, winner = gomoku.get_game_status()
            if ended == 1:
                break

            # update tree search
            mcts_best.update_with_move(best_move)

            # next player
            player_index = -player_index

        print("human win" if winner == human_color else "alpha win")

        t.join()

    def load_samples(self, folder="models", filename="checkpoint.example"):
        """load self.examples_buffer
        """

        filepath = path.join(folder, filename)
        with open(filepath, 'rb') as f:
            self.examples_buffer = pickle.load(f)

    def save_samples(self, folder="models", filename="checkpoint.example"):
        """save self.examples_buffer
        """

        if not path.exists(folder):
            mkdir(folder)

        filepath = path.join(folder, filename)
        with open(filepath, 'wb') as f:
            pickle.dump(self.examples_buffer, f, -1)
