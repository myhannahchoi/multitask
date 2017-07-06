"""
Analysis of general properties of tasks
"""

from __future__ import division

import os
import numpy as np
import pickle
import matplotlib.pyplot as plt
import tensorflow as tf
from tensorflow.python.ops import rnn
from tensorflow.python.ops import init_ops
from tensorflow.python.client.session import Session

from task import *
from network import LeakyRNNCell, popvec

fast_eval = True

class Run(Session):
    '''
    A class for running the network.
    This modified class is initialized with the model sctructure.
    It is less flexible than raw tensorflow,
    but more convenient if you want to reuse the same model over and over again.
    '''
    def __init__(self, restore=None, config=None, sigma_rec=None,
                 lesion_units=None, fast_eval=False):
        '''
        save_addon: add on for loading and saving
        inh_id    : Ids of units to inhibit inputs or outputs
        inh_output: If True, the inhibit the output of neurons, otherwise inhibit inputs to neurons
        '''

        # Reset tensorflow graphs
        tf.reset_default_graph() # must be in the beginning

        # Initialize tensorflow session
        super(Run, self).__init__() # initialize Session()

        if restore is not None:
            save_addon = restore
            assert config is None
            print('Loading network ' + save_addon)
            # Load config
            with open(os.path.join('data','config'+save_addon+'.pkl'),'rb') as f:
                config = pickle.load(f)

            # Only if restoring previous models
            if fast_eval: # Evaluate at a bigger time step
                config['dt']    = 10
                # print('Currently using fast evaluation')
            else:
                config['dt']    = 1

            # Temporary for backward-compatibility
            if 'activation' not in config:
                config['activation'] = 'softplus'

            if 'w_rec_init' not in config:
                config['w_rec_init'] = 'diag'

            if 'beta_anchor' not in config:
                config['beta_anchor'] = 0.0

            if 'l_ewc' not in config:
                config['l_ewc'] = 0.0

            if 'c_intsyn' not in config:
                config['c_intsyn'] = 0.0

            if 'loss_type' not in config:
                config['loss_type'] = 'lsq'
        else:
            assert config is not None

        config['alpha'] = config['dt']/TAU

        if config['seed'] is not None:
            tf.set_random_seed(config['seed'])

        if sigma_rec is not None:
            print('Overwrite original sigma_rec with {:0.3f}'.format(sigma_rec))
            config['sigma_rec'] = sigma_rec

        # Network Parameters
        n_input, n_hidden, n_output = config['shape']

        # tf Graph input
        self.x = tf.placeholder("float", [None, None, n_input]) # time * batch * n_input

        # TEMPORARY: From train.py
        self.y = tf.placeholder("float", [None, n_output])
        if config['loss_type'] == 'lsq':
            self.c_mask = tf.placeholder("float", [None, n_output])
        else:
            # Mask on time
            self.c_mask = tf.placeholder("float", [None])

        # Define weights
        if config['activation'] == 'softplus':
            stddev = 0.4/np.sqrt(n_hidden)
        elif config['activation'] == 'relu':
            stddev = 0.6/np.sqrt(n_hidden)
        elif config['activation'] == 'tanh':
            stddev = 2.0/np.sqrt(n_hidden)
        elif config['activation'] == 'suplin':
            stddev = 0.2/np.sqrt(n_hidden)

        # w_out = tf.Variable(tf.random_normal([n_hidden, n_output], stddev=stddev, name='Matrix'))
        # b_out = tf.Variable(tf.zeros([n_output]), name='Bias')


        with tf.variable_scope("Output"):
            w_out = tf.get_variable('Matrix', [n_hidden, n_output], dtype=tf.float32,
                                    initializer=init_ops.random_normal_initializer(0.0, stddev, dtype=tf.float32))
            b_out = tf.get_variable('Bias', [n_output], dtype=tf.float32,
                                    initializer=init_ops.constant_initializer(0.0, dtype=tf.float32))

        # Initial state (requires tensorflow later than 0.10)
        h_init = tf.Variable(0.3*tf.ones([1, n_hidden]), name='InitActivity')
        h_init_bc = tf.tile(h_init, [tf.shape(self.x)[1], 1]) # broadcast to size (batch, n_h)

        # Recurrent activity
        cell = LeakyRNNCell(n_hidden, config['alpha'], sigma_rec=config['sigma_rec'],
                            activation=config['activation'], w_rec_init=config['w_rec_init'],
                            rng=config['rng'])

        self.h, states = rnn.dynamic_rnn(cell, self.x, initial_state=tf.abs(h_init_bc),
                                    dtype=tf.float32, time_major=True) # time_major is important

        # Output
        if config['loss_type'] == 'lsq':
            y_hat = tf.sigmoid(tf.matmul(tf.reshape(self.h, (-1, n_hidden)), w_out) + b_out)
            # Loss
            self.cost_lsq = tf.reduce_mean(tf.square((self.y-y_hat)*self.c_mask))
        else:
            # y_hat_ shape (n_time*n_batch, n_unit)
            y_hat_ = tf.matmul(tf.reshape(self.h, (-1, n_hidden)), w_out) + b_out
            y_hat = tf.nn.softmax(y_hat_)
            # Actually the cross-entropy cost
            self.cost_lsq = tf.reduce_mean(self.c_mask*
                                           tf.nn.softmax_cross_entropy_with_logits(labels=self.y, logits=y_hat_))


        self.grad_lsq = tf.gradients(self.cost_lsq, tf.trainable_variables())

        # Including a weak L1 regularization on activity to prevent blowing up
        self.cost_reg = tf.reduce_mean(tf.abs(self.h))*0.0001 # Regularizer
        # TODO: TEMPORARY
        # self.cost_reg = tf.reduce_mean(tf.abs(h))*1. # Regularizer

        # Create an optimizer.
        self.opt = tf.train.AdamOptimizer(learning_rate=config['learning_rate'])

        self.grads_and_vars = self.opt.compute_gradients(self.cost_lsq+self.cost_reg, tf.trainable_variables())
        capped_gvs = [(tf.clip_by_value(grad, -1., 1.), var) for grad, var in self.grads_and_vars]
        # self.glsq = tf.gradients(self.cost_lsq, tf.trainable_variables())

        # self.glsq_v = self.opt.compute_gradients(self.cost_lsq, tf.trainable_variables())
        # self.greg_v = self.opt.compute_gradients(self.cost_reg, tf.trainable_variables())
        # capped_gvs = list()
        # for aa, bb in zip(self.glsq_v, self.greg_v):
        #     if bb[0] is None:
        #         capped_gvs.append((tf.clip_by_value(aa[0], -1., 1.), aa[1]))
        #     else:
        #         capped_gvs.append((tf.clip_by_value(aa[0]+bb[0], -1., 1.), aa[1]))
        self.optimizer = self.opt.apply_gradients(capped_gvs)


        init = tf.global_variables_initializer()
        self.run(init)

        # Restore variable
        saver = tf.train.Saver()

        if restore is not None:
            saver.restore(self, os.path.join('data', config['save_addon']+'.ckpt'))

        if lesion_units is not None:
            try:
                _ = iter(lesion_units)
                lesion_units = np.array(lesion_units)
            except TypeError:
                lesion_units = np.array([lesion_units])
            print('Lesioning Units:')
            print(lesion_units)

            # Temporary solution before better ways to get recurrent connections
            w_rec = self.run(tf.trainable_variables()[3])
            assert w_rec.shape==(n_input+n_hidden, n_hidden) # has to be the recurrent connection
            w_rec[n_input+lesion_units, :] = 0 # Set output projections from these units to zero
            lesion_w_rec = tf.trainable_variables()[3].assign(w_rec)
            self.run(lesion_w_rec)

            w_out = self.run(tf.trainable_variables()[0])
            assert w_out.shape==(n_hidden, n_output) # has to be the recurrent connection
            w_out[lesion_units, :] = 0 # Set output projections from these units to zero
            lesion_w_out = tf.trainable_variables()[0].assign(w_out)
            self.run(lesion_w_out)


        self.var_list   = tf.trainable_variables()

        self.f_h        = lambda x0 : self.run(self.h, feed_dict={self.x : x0})
        self.f_y        = lambda h0 : self.run(y_hat, feed_dict={self.h : h0}).reshape((h0.shape[0],h0.shape[1],n_output))
        self.f_y_from_x = lambda x0 : self.f_y(self.f_h(x0))
        self.f_y_loc    = lambda y0 : popvec(y0[...,1:])
        self.f_y_loc_from_x = lambda x0 : self.f_y_loc(self.f_y(self.f_h(x0)))
        self.f_cost     = lambda y0, y_hat0, c_mask0 : np.mean(np.sum((c_mask0*(y_hat0-y0))**2),axis=0)
        self.f_grad     = lambda x0, y0, c_mask0 : self.run(
            self.grads_and_vars, feed_dict={self.x: x0, self.y: y0, self.c_mask: c_mask0})
        self.f_vars     = lambda : self.run(tf.trainable_variables())

        # Without continual learning
        # self.train_one_step = lambda x0, y0, c_mask0 : self.run(self.optimizer,
        # feed_dict={self.x: x0, self.y: y0, self.c_mask: c_mask0})

        # Notice this weight is originally used as r*W, so transpose them
        self.params = self.run(tf.trainable_variables())
        self.w_out = self.params[0].T
        self.b_out = self.params[1]
        self.h_init= abs(self.params[2][0,:])
        self.w_rec = self.params[3][-n_hidden:, :].T
        self.w_in  = self.params[3][:n_input, :].T
        self.b_rec = self.params[4]

        self.config = config
        self.saver  = saver

        self.test_ran = False

    def save(self):
        save_path = self.saver.save(self, os.path.join('data', self.config['save_addon']+'.ckpt'))
        print("Model saved in file: %s" % save_path)

    def update_cost(self, anchor_weights, anchor_vars, factor, reset=False):

        if reset:
            self.cost_reg = tf.reduce_mean(tf.abs(self.h))*0.0001 # Regularizer

        for v, w, v_val in zip(self.var_list, anchor_weights, anchor_vars):
            self.cost_reg += factor*tf.reduce_sum(tf.multiply(w, tf.square(v-v_val)))

        self.grads_and_vars = self.opt.compute_gradients(self.cost_lsq+self.cost_reg, tf.trainable_variables())
        capped_gvs = [(tf.clip_by_value(grad, -1., 1.), var) for grad, var in self.grads_and_vars]
        self.optimizer = self.opt.apply_gradients(capped_gvs)
        # self.grads_lsq_and_vars = self.opt.compute_gradients(self.cost_lsq, tf.trainable_variables())

        # self.glsq_v = self.opt.compute_gradients(self.cost_lsq, tf.trainable_variables())
        # self.greg_v = self.opt.compute_gradients(self.cost_reg, tf.trainable_variables())
        # capped_gvs = [(tf.clip_by_value(self.glsq_v[i][0]+self.greg_v[i][0], -1., 1.), self.glsq_v[i][1]) for i in range(len(self.glsq_v))]
        # self.optimizer = self.opt.apply_gradients(capped_gvs)

def test_init():
    N_RING = 32
    num_ring = 2
    HDIM = 300
    seed = 1
    rng  = np.random.RandomState(seed)
    config = {'h_type'      : 'leaky_rec',
              'loss_type'   : 'crossentropy',
              'activation'  : 'relu',
              'alpha'       : 0.2, # \Delta t/tau
              'dt'          : 0.2*TAU,
              'sigma_rec'   : 0.15,
              'sigma_x'     : 0.01,
              'w_rec_init'  : 'randgauss',
              'seed'        : seed,
              'rng'         : rng,
              'beta_anchor' : 0.0,
              'HDIM'        : HDIM,
              'N_RING'      : N_RING,
              'num_ring'    : num_ring,
              'rule_start'  : 1+num_ring*N_RING,
              'shape'       : (1+num_ring*N_RING+N_RULE, HDIM, N_RING+1),
              'save_addon'  : 'init',
              'rules'       : [CHOICEATTEND_MOD1],
              'rule_weights': None,
              'learning_rate': 0.001,
              'training_iters' : 100,
              'batch_size_train' : 10,
              'batch_size_test' : 10}

    task = generate_onebatch(rule=CHOICEATTEND_MOD1, config=config, mode='sample', t_tot=1000)
    with Run(config=config) as R:
        n_input, n_hidden, n_output = config['shape']
        h_sample = R.f_h(task.x)
        y_sample = R.f_y(h_sample)
        # grads_and_vars = R.f_grad(task.x,
        #                task.y.reshape((-1,n_output)),
        #                task.c_mask.reshape((-1,n_output)))
        w_rec = R.w_rec

        # Saving the model
        R.save()

        with open(os.path.join('data', 'config'+config['save_addon']+'.pkl'), 'wb') as f:
            pickle.dump(config, f)

    plt.plot(task.x[:,0,:])
    plt.show()

    plt.plot(h_sample[:,0,:])
    plt.show()
    
    plt.hist(h_sample[:,0,:].flatten())
    plt.show()

    plt.plot(y_sample[:,0,:])
    plt.show()

    plt.plot(y_sample[:,0,:].sum(axis=-1))
    plt.show()

    plt.plot(task.y[:,0,:].sum(axis=-1))
    plt.show()

    plt.hist(y_sample[:,0,:].flatten())
    plt.show()

    w, v = np.linalg.eig(w_rec-np.eye(w_rec.shape[0]))
    plt.figure()
    plt.scatter(np.real(w), np.imag(w))

    
def replacerule(R, rule, rule_X, beta):
    '''
    Run the network but with replaced rule input weight
    :param rule: the rule to run
    :param rule_X: A numpy array of rules, whose values will be used to replace
    :param beta: the weights for each rule_X vector used.
    If beta='fit', use the best linear fit

    The rule input connection will be replaced by
    sum_i rule_connection(rule_X_i) * beta_i
    '''

    ## TEMPORARY FOR BACKWARD COMPATIBILITY
    if 'num_ring' not in R.config:
            R.config['num_ring'] = 2 # default number

    if 'rule_start' not in R.config:
        R.config['rule_start'] = 1+R.config['num_ring']*R.config['N_RING']

    # Get current connectivity
    # This gives w_input and w_rec
    w_rec_ = R.run(tf.trainable_variables()[3])

    # Update connectivity
    rule_y = np.array([rule])
    w_rule_X = w_rec_[R.config['rule_start']+rule_X, :]
    w_rule_y = w_rec_[R.config['rule_start']+rule_y, :]

    if beta is 'fit':
        # Best linear fit
        beta = np.dot(w_rule_y, np.linalg.pinv(w_rule_X))

    w_rec_[R.config['rule_start']+rule, :] = np.dot(beta, w_rule_X)
    change_w_rec = tf.trainable_variables()[3].assign(w_rec_)
    R.run(change_w_rec)

    return beta

def sample_plot(save_addon, rule, save=False, plot_ylabel=False):
    import seaborn.apionly as sns
    fs = 7

    with Run(save_addon, fast_eval=True) as R:
        config = R.config
        task = generate_onebatch(rule=rule, config=config, mode='sample', t_tot=2000)
        x_sample = task.x
        h_sample = R.f_h(x_sample)
        y_sample = R.f_y(h_sample)

        params = R.params
        w_rec = R.w_rec
        w_in  = R.w_in

    t_plot = np.arange(x_sample.shape[0])*config['dt']/1000

    assert config['num_ring'] == 2

    # y_sample = y_sample.reshape((-1,1,config['shape'][2]))

    y = task.y

    N_RING = config['N_RING']

    fig = plt.figure(figsize=(1.3,2))
    ylabels = ['fix. in', 'stim. mod1', 'stim. mod2','fix. out', 'out']
    heights = np.array([0.03,0.2,0.2,0.03,0.2])+0.01
    for i in range(5):
        ax = fig.add_axes([0.15,sum(heights[i+1:]+0.02)+0.1,0.8,heights[i]])
        cmap = sns.cubehelix_palette(light=1, as_cmap=True, rot=0)
        plt.xticks([])
        ax.tick_params(axis='both', which='major', labelsize=fs, width=0.5, length=2, pad=3)

        if plot_ylabel:
            ax.spines["right"].set_visible(False)
            ax.spines["bottom"].set_visible(False)
            ax.spines["top"].set_visible(False)
            ax.xaxis.set_ticks_position('bottom')
            ax.yaxis.set_ticks_position('left')

        else:
            ax.spines["left"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["bottom"].set_visible(False)
            ax.spines["top"].set_visible(False)
            ax.xaxis.set_ticks_position('none')

        if i == 0:
            plt.plot(t_plot, x_sample[:,0,0], color=sns.xkcd_palette(['blue'])[0])
            if plot_ylabel:
                plt.yticks([0,1],['',''],rotation='vertical')
            plt.ylim([-0.1,1.5])
            plt.title(rule_name[rule],fontsize=fs)
        elif i == 1:
            plt.imshow(x_sample[:,0,1:1+N_RING].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
            if plot_ylabel:
                plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$',r'180$\degree$',r'360$\degree$'],rotation='vertical')
        elif i == 2:
            plt.imshow(x_sample[:,0,1+N_RING:1+2*N_RING].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
            # plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$',r'180$\degree$',r'360$\degree$'],rotation='vertical')
            if plot_ylabel:
                plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$',r'180$\degree$',r'360$\degree$'],rotation='vertical')
        elif i == 3:
            plt.plot(t_plot, y[:,0,0],color=sns.xkcd_palette(['green'])[0])
            plt.plot(t_plot, y_sample[:,0,0],color=sns.xkcd_palette(['blue'])[0])
            if plot_ylabel:
                plt.yticks([0.05,0.8],['',''],rotation='vertical')
            plt.ylim([-0.1,1.1])
        elif i == 4:
            plt.imshow(y_sample[:,0,1:].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
            # plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$',r'180$\degree$',r'360$\degree$'],rotation='vertical')
            if plot_ylabel:
                plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$',r'180$\degree$',r'360$\degree$'],rotation='vertical')
            plt.xticks([0,y_sample.shape[0]], ['0', '2'])
            plt.xlabel('Time (s)',fontsize=fs, labelpad=-3)
            ax.spines["bottom"].set_visible(True)

        if plot_ylabel:
           plt.ylabel(ylabels[i],fontsize=fs)
        else:
            plt.yticks([])
        ax.get_yaxis().set_label_coords(-0.12,0.5)

    if save:
        plt.savefig('figure/sample_'+rule_name[rule].replace(' ','')+'.pdf', transparent=True)
    plt.show()

    # plt.figure()
    # _ = plt.plot(h_sample[:,0,:20])
    # plt.show()
    #
    # plt.figure()
    # _ = plt.plot(y_sample[:,0,:])
    # plt.show()

def schematic_plot(save_addon):
    import seaborn.apionly as sns
    fontsize = 6

    rule = CHOICE_MOD1

    with Run(save_addon) as R:
        config = R.config
        task = generate_onebatch(rule=rule, config=config, mode='sample', t_tot=1000)
        x_sample = task.x
        h_sample = R.f_h(x_sample)
        y_sample = R.f_y(h_sample)


    N_RING = config['N_RING']

    # Plot Stimulus
    fig = plt.figure(figsize=(1.0,1.2))
    heights = np.array([0.06,0.25,0.25])
    for i in range(3):
        ax = fig.add_axes([0.2,sum(heights[i+1:]+0.1)+0.05,0.7,heights[i]])
        cmap = sns.cubehelix_palette(light=1, as_cmap=True, rot=0)
        plt.xticks([])

        # Fixed style for these plots
        ax.tick_params(axis='both', which='major', labelsize=fontsize, width=0.5, length=2, pad=3)
        ax.spines["left"].set_linewidth(0.5)
        ax.spines["right"].set_visible(False)
        ax.spines["bottom"].set_visible(False)
        ax.spines["top"].set_visible(False)
        ax.xaxis.set_ticks_position('bottom')
        ax.yaxis.set_ticks_position('left')

        if i == 0:
            plt.plot(x_sample[:,0,0], color=sns.xkcd_palette(['blue'])[0])
            plt.yticks([0,1],['',''],rotation='vertical')
            plt.ylim([-0.1,1.5])
            plt.title('Fixation input', fontsize=fontsize, y=0.9)
        elif i == 1:
            plt.imshow(x_sample[:,0,1:1+N_RING].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
            plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$','',r'360$\degree$'],rotation='vertical')
            plt.title('Stimulus mod 1', fontsize=fontsize, y=0.9)
        elif i == 2:
            plt.imshow(x_sample[:,0,1+N_RING:1+2*N_RING].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
            plt.yticks([0,(N_RING-1)/2,N_RING-1],['','',''],rotation='vertical')
            plt.title('Stimulus mod 2', fontsize=fontsize, y=0.9)
        ax.get_yaxis().set_label_coords(-0.12,0.5)
    plt.savefig('figure/schematic_input.pdf',transparent=True)
    plt.show()

    # Plot Rule Inputs
    fig = plt.figure(figsize=(1.0, 0.5))
    ax = fig.add_axes([0.2,0.3,0.7,0.45])
    cmap = sns.cubehelix_palette(light=1, as_cmap=True, rot=0)
    X = x_sample[:,0,1+2*N_RING:]
    plt.imshow(X.T, aspect='auto', vmin=0, vmax=1, cmap=cmap, interpolation='none',origin='lower')

    plt.xticks([0, 1000])
    ax.set_xlabel('Time (ms)', fontsize=fontsize, labelpad=-5)

    # Fixed style for these plots
    ax.tick_params(axis='both', which='major', labelsize=fontsize, width=0.5, length=2, pad=3)
    ax.spines["left"].set_linewidth(0.5)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_linewidth(0.5)
    ax.spines["top"].set_visible(False)
    ax.xaxis.set_ticks_position('bottom')
    ax.yaxis.set_ticks_position('left')

    plt.yticks([0,X.shape[-1]-1],['1',str(X.shape[-1])],rotation='vertical')
    plt.title('Rule inputs', fontsize=fontsize, y=0.9)
    ax.get_yaxis().set_label_coords(-0.12,0.5)

    plt.savefig('figure/schematic_rule.pdf',transparent=True)
    plt.show()


    # Plot Units
    fig = plt.figure(figsize=(1.0, 0.8))
    ax = fig.add_axes([0.2,0.1,0.7,0.75])
    cmap = sns.cubehelix_palette(light=1, as_cmap=True, rot=0)
    plt.xticks([])
    # Fixed style for these plots
    ax.tick_params(axis='both', which='major', labelsize=fontsize, width=0.5, length=2, pad=3)
    ax.spines["left"].set_linewidth(0.5)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.spines["top"].set_visible(False)
    ax.xaxis.set_ticks_position('bottom')
    ax.yaxis.set_ticks_position('left')

    plt.imshow(h_sample[:,0,:].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
    plt.yticks([0,config['HDIM']-1],['1',str(config['HDIM'])],rotation='vertical')
    plt.title('Network units', fontsize=fontsize, y=0.95)
    ax.get_yaxis().set_label_coords(-0.12,0.5)
    plt.savefig('figure/schematic_units.pdf',transparent=True)
    plt.show()


    # Plot Outputs
    fig = plt.figure(figsize=(1.0,0.8))
    heights = np.array([0.1,0.45])+0.01
    for i in range(2):
        ax = fig.add_axes([0.2,sum(heights[i+1:]+0.15)+0.1,0.7,heights[i]])
        cmap = sns.cubehelix_palette(light=1, as_cmap=True, rot=0)
        plt.xticks([])

        # Fixed style for these plots
        ax.tick_params(axis='both', which='major', labelsize=fontsize, width=0.5, length=2, pad=3)
        ax.spines["left"].set_linewidth(0.5)
        ax.spines["right"].set_visible(False)
        ax.spines["bottom"].set_visible(False)
        ax.spines["top"].set_visible(False)
        ax.xaxis.set_ticks_position('bottom')
        ax.yaxis.set_ticks_position('left')

        if i == 0:
            # plt.plot(task.y[:,0,0],color=sns.xkcd_palette(['green'])[0])
            plt.plot(y_sample[:,0,0],color=sns.xkcd_palette(['blue'])[0])
            plt.yticks([0.05,0.8],['',''],rotation='vertical')
            plt.ylim([-0.1,1.1])
            plt.title('Fixation output', fontsize=fontsize, y=0.9)

        elif i == 1:
            plt.imshow(y_sample[:,0,1:].T, aspect='auto', cmap=cmap, vmin=0, vmax=1, interpolation='none',origin='lower')
            plt.yticks([0,(N_RING-1)/2,N_RING-1],[r'0$\degree$','',r'360$\degree$'],rotation='vertical')
            plt.xticks([])
            plt.title('Response', fontsize=fontsize, y=0.9)

        ax.get_yaxis().set_label_coords(-0.12,0.5)

    plt.savefig('figure/schematic_outputs.pdf',transparent=True)
    plt.show()

def plot_singleneuron_intime(save_addon, neurons, rules,
                             epoch=None, save=False, ylabel_firstonly=True,
                             trace_only=False, plot_stim_avg=False):
    '''

    :param save_addon:
    :param neurons: indices of neurons to plot
    :param rules: rules to plot
    :param epoch: epoch to plot
    :param save: save figure?
    :param ylabel_firstonly: if True, only plot ylabel for the first rule in rules
    :return:
    '''
    import seaborn.apionly as sns
    try:
        _ = iter(rules)
    except TypeError:
        rules = [rules]

    try:
        _ = iter(neurons)
    except TypeError:
        neurons = [neurons]

    h_tests = dict()
    with Run(save_addon, fast_eval=fast_eval) as R:
        config = R.config
        t_start = int(500/config['dt'])

        for rule in rules:
            task = generate_onebatch(rule=rule, config=config, mode='test')
            h_tests[rule] = R.f_h(task.x) # (Time, Batch, Units)

    for neuron in neurons:
        h_max = np.max([h_tests[r][t_start:,:,neuron].max() for r in rules])
        for j, rule in enumerate(rules):
            fs = 6
            fig = plt.figure(figsize=(1.0,0.8))
            ax = fig.add_axes([0.35,0.25,0.55,0.55])
            # ax.set_color_cycle(sns.color_palette("husl", h_tests[rule].shape[1]))
            t_plot = np.arange(h_tests[rule][t_start:].shape[0])*config['dt']/1000
            _ = ax.plot(t_plot,
                        h_tests[rule][t_start:,:,neuron], lw=0.5, color='gray')

            if plot_stim_avg:
                # Plot stimulus averaged trace
                _ = ax.plot(np.arange(h_tests[rule][t_start:].shape[0])*config['dt']/1000,
                        h_tests[rule][t_start:,:,neuron].mean(axis=1), lw=1, color='black')

            if epoch is not None:
                e0, e1 = task.epochs[epoch]
                e0 = e0 if e0 is not None else 0
                e1 = e1 if e1 is not None else h_tests[rule].shape[0]
                ax.plot([e0, e1], [h_max*1.15]*2,
                        color='black',linewidth=1.5)
                save_name = 'figure/trace_'+rule_name[rule]+epoch+save_addon+'.pdf'
            else:
                save_name = 'figure/trace_unit'+str(neuron)+rule_name[rule]+save_addon+'.pdf'

            plt.ylim(np.array([-0.1, 1.2])*h_max)
            plt.xticks([0,np.floor(np.max(t_plot)+0.01)])
            plt.xlabel('Time (s)', fontsize=fs, labelpad=-5)
            plt.locator_params(axis='y', nbins=4)
            if j>0 and ylabel_firstonly:
                ax.set_yticklabels([])
            else:
                plt.ylabel('Activitity (a.u.)', fontsize=fs, labelpad=2)
            plt.title('Unit {:d} '.format(neuron) + rule_name[rule], fontsize=5)
            ax.tick_params(axis='both', which='major', labelsize=fs)
            ax.spines["right"].set_visible(False)
            ax.spines["top"].set_visible(False)
            ax.xaxis.set_ticks_position('bottom')
            ax.yaxis.set_ticks_position('left')
            if trace_only:
                ax.spines["left"].set_visible(False)
                ax.spines["bottom"].set_visible(False)
                ax.xaxis.set_ticks_position('none')
                ax.set_xlabel('')
                ax.set_ylabel('')
                ax.set_xticks([])
                ax.set_yticks([])
                ax.set_title('')

            if save:
                plt.savefig(save_name, transparent=True)
            plt.show()


if __name__ == "__main__":
    # schematic_plot(save_addon='allrule_softplus_0_256paper')
    rules = range(N_RULE)

    # rules = [FDGO, DELAYGO, FDANTI, DELAYANTI]
    for rule in rules:
        pass
        # sample_plot(save_addon='allrule_softplus_0_256paper', rule=rule, save=True)

#==============================================================================
#     plot_singleneuron_intime('allrule_softplus_0_300test', [16], [FDGO],
#                              epoch=None, save=False, trace_only=True, plot_stim_avg=True)
#==============================================================================


    # test_init()
    pass


