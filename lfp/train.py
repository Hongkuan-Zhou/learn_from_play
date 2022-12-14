import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow.python.keras import mixed_precision
from tensorflow.python.keras.optimizers import adam_v2

tfd = tfp.distributions
tfpl = tfp.layers
import lfp
import os

from lfp.metric import record, log_action_breakdown


class BetaScheduler():
    def __init__(self, schedule='constant', beta=0.0, beta_max=1.0, max_steps=1e4,
                 cycles=10, duty_cycle=0.5, plot=True):
        self.schedule = schedule
        self.beta_min = beta
        self.beta_max = beta_max
        self.max_steps = max_steps
        self.cycles = cycles
        self.duty_cycle = duty_cycle

        if schedule == 'constant':
            self.scheduler = lambda s: tf.ones_like(s, dtype=tf.float32) * beta
        elif schedule == 'linear':
            self.scheduler = self.linear_schedule
        elif schedule == 'quadratic':
            self.scheduler = self.quadratic_schedule
        elif schedule == 'cyclic':
            self.scheduler = self.cyclic_schedule
        else:
            raise NotImplementedError()
        if plot: self._plot_schedule()

    def linear_schedule(self, step):
        beta = self.beta_min + (step) * (self.beta_max - self.beta_min) / self.max_steps
        return tf.clip_by_value(float(beta), self.beta_min, self.beta_max, name='beta_linear')

    def quadratic_schedule(self, step):
        ''' y = (b1-b0)/n^2 * x^2 + b0 '''
        beta = self.beta_min + (step) ** 2 * (self.beta_max - self.beta_min) / self.max_steps ** 2
        return tf.clip_by_value(float(beta), self.beta_min, self.beta_max, name='beta_quadratic')

    def cyclic_schedule(self, step):
        period = self.max_steps // self.cycles
        step = step % period  # map step to cycle
        if step < period * self.duty_cycle:
            # linear regime
            beta = self.beta_min + (step) * (self.beta_max - self.beta_min) / (period * self.duty_cycle)
        else:
            # constant regime
            beta = self.beta_max
        return tf.clip_by_value(float(beta), self.beta_min, self.beta_max, name='beta_cyclic')

    def _plot_schedule(self):
        ts = np.arange(self.max_steps, step=100)
        plt.plot(ts, [self.scheduler(t) for t in ts])
        plt.xlabel('Steps')
        plt.ylabel('Beta')


class cosineDecay():
    def __init__(self, min_frac=1 / 16, max=1.0, decay_steps=20000):  # min is as a fraction
        self.min_frac = min_frac
        self.max = max
        self.decay_steps = decay_steps

    def schedule(self, step):
        step = min(step, self.decay_steps)
        cosine_decay = 0.5 * (1 + np.cos(np.pi * step / self.decay_steps))
        decayed = (1 - self.min_frac) * cosine_decay + self.min_frac
        return self.max * decayed

    def _plot_schedule(self):
        ts = np.arange(self.decay_steps * 2, step=100)
        plt.plot(ts, [self.schedule(t) for t in ts])
        plt.xlabel('Steps')
        plt.ylabel('Beta')


class LFPTrainer():

    def __init__(self, args, actor, dl, encoder=None, planner=None, cnn=None, gripper_cnn=None,
                 img_embed_to_goal_space=None, lang_embed_to_goal_space=None, \
                 optimizer=adam_v2.Adam, strategy=None, global_batch_size=32):

        self.actor = actor
        self.encoder = encoder
        self.planner = planner
        self.cnn = cnn
        self.gripper_cnn = gripper_cnn
        self.img_embed_to_goal_space = img_embed_to_goal_space
        self.lang_embed_to_goal_space = lang_embed_to_goal_space
        self.strategy = strategy
        self.args = args
        self.dl = dl
        self.global_batch_size = global_batch_size

        if args.fp16:
            optimizer = mixed_precision.LossScaleOptimizer(optimizer)

        if self.args.num_distribs is None:  # different sized clips due to different sized losses
            actor_clip = 0.06
            encoder_clip = 0.03
            planner_clip = 0.001
            cnn_clip = 10  # TODO find value if doing non de
            gripper_cnn_clip = 10.0
            mapper_clip = 5.0
        else:
            actor_clip = 400.0
            encoder_clip = 30.0
            planner_clip = 5.0
            cnn_clip = 20.0
            gripper_cnn_clip = 10.0
            mapper_clip = 5.0

        self.temperature = args.temperature
        self.temp_schedule = cosineDecay(min_frac=1 / 16, max=args.temperature, decay_steps=20000)

        # bit boiler platy having them all separate, but I tried a really clean dicts+comprehensions method and the TPU complained about having non XLA functions - so it stays this way for now. 
        self.actor_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=actor_clip)
        self.encoder_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=encoder_clip)
        self.planner_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=planner_clip)
        self.cnn_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=cnn_clip)
        self.gripper_cnn_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=gripper_cnn_clip)
        self.img_embed_to_goal_space_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=mapper_clip)
        self.lang_embed_to_goal_space_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=mapper_clip)
        self.VQ_optimizer = optimizer(learning_rate=args.learning_rate, clipnorm=encoder_clip)

        self.nll_action_loss = lambda y, p_y: tf.reduce_sum(-p_y.log_prob(y), axis=2)
        self.mae_action_loss = tf.keras.losses.MeanAbsoluteError(reduction=tf.keras.losses.Reduction.NONE)
        self.mse_action_loss = tf.keras.losses.MeanSquaredError(reduction=tf.keras.losses.Reduction.NONE)

        self.metrics = {}
        self.metrics['train_loss'] = tf.keras.metrics.Mean(name='train_loss')
        self.metrics['valid_loss'] = tf.keras.metrics.Mean(name='valid_loss')
        self.metrics['actor_grad_norm'] = tf.keras.metrics.Mean(name='actor_grad_norm')
        self.metrics['encoder_grad_norm'] = tf.keras.metrics.Mean(name='encoder_grad_norm')
        self.metrics['planner_grad_norm'] = tf.keras.metrics.Mean(name='planner_grad_norm')
        self.metrics['cnn_grad_norm'] = tf.keras.metrics.Mean(name='cnn_grad_norm')
        self.metrics['gripper_cnn_grad_norm'] = tf.keras.metrics.Mean(name='gripper_cnn_grad_norm')
        self.metrics['img_embed_to_goal_space_norm'] = tf.keras.metrics.Mean(name='img_embed_to_goal_space_norm')
        self.metrics['lang_embed_to_goal_space_norm'] = tf.keras.metrics.Mean(name='lang_embed_to_goal_space_norm')
        self.metrics['VQ_grad_norm'] = tf.keras.metrics.Mean(name='VQ_grad_norm')

        self.metrics['global_grad_norm'] = tf.keras.metrics.Mean(name='global_grad_norm')

        self.metrics['train_act_with_enc_loss'] = tf.keras.metrics.Mean(name='train_act_with_enc_loss')
        self.metrics['train_act_with_plan_loss'] = tf.keras.metrics.Mean(name='train_act_with_plan_loss')
        self.metrics['valid_act_with_enc_loss'] = tf.keras.metrics.Mean(name='valid_act_with_enc_loss')
        self.metrics['valid_act_with_plan_loss'] = tf.keras.metrics.Mean(name='valid_act_with_plan_loss')

        self.metrics['train_reg_loss'] = tf.keras.metrics.Mean(name='reg_loss')
        self.metrics['valid_reg_loss'] = tf.keras.metrics.Mean(name='valid_reg_loss')

        self.metrics['train_discrete_planner_loss'] = tf.keras.metrics.Mean(name='train_discrete_planner_loss')
        self.metrics['valid_discrete_planner_loss'] = tf.keras.metrics.Mean(name='valid_discrete_planner_loss')

        self.metrics['commitment_loss'] = tf.keras.metrics.Mean(name='commitment_loss')
        self.metrics['entropy'] = tf.keras.metrics.Mean(name='entropy')
        self.metrics['train_discrete_planner_acc'] = tf.keras.metrics.Mean(name='train_discrete_planner_acc')
        self.metrics['valid_discrete_planner_acc'] = tf.keras.metrics.Mean(name='valid_discrete_planner_acc')

        self.metrics['valid_position_loss'] = tf.keras.metrics.Mean(name='valid_position_loss')
        self.metrics['valid_max_position_loss'] = lfp.metric.MaxMetric(name='valid_max_position_loss')
        self.metrics['valid_rotation_loss'] = tf.keras.metrics.Mean(name='valid_rotation_loss')
        self.metrics['valid_max_rotation_loss'] = lfp.metric.MaxMetric(name='valid_max_rotation_loss')
        self.metrics['valid_gripper_loss'] = tf.keras.metrics.Mean(name='valid_rotation_loss')

        self.metrics['valid_enc_position_loss'] = tf.keras.metrics.Mean(name='valid_enc_position_loss')
        self.metrics['valid_enc_max_position_loss'] = lfp.metric.MaxMetric(name='valid_enc_max_position_loss')
        self.metrics['valid_enc_rotation_loss'] = tf.keras.metrics.Mean(name='valid_enc_rotation_loss')
        self.metrics['valid_enc_max_rotation_loss'] = lfp.metric.MaxMetric(name='valid_enc_max_rotation_loss')
        self.metrics['valid_enc_gripper_loss'] = tf.keras.metrics.Mean(name='valid_enc_rotation_loss')

        self.metrics['valid_lang_position_loss'] = tf.keras.metrics.Mean(name='valid_position_loss')
        self.metrics['valid_lang_max_position_loss'] = lfp.metric.MaxMetric(name='valid_max_position_loss')
        self.metrics['valid_lang_rotation_loss'] = tf.keras.metrics.Mean(name='valid_rotation_loss')
        self.metrics['valid_lang_max_rotation_loss'] = lfp.metric.MaxMetric(name='valid_max_rotation_loss')
        self.metrics['valid_lang_gripper_loss'] = tf.keras.metrics.Mean(name='valid_rotation_loss')

        self.chkpt_manager = None

        if self.args.discrete:
            if self.args.vq_ema:
                self.VQ = lfp.VQ.VQ_EMA(self.args, commitment_cost=args.commit_cost)
            else:
                self.VQ = lfp.VQ.VQ_GRAD(self.args, commitment_cost=args.commit_cost)

    def update_schedules(self, step):
        self.temperature = self.temp_schedule.schedule(step)

    def compute_loss(self, labels, predictions, mask, seq_lens, weightings=None):
        if self.args.num_distribs is not None:
            per_example_loss = self.nll_action_loss(labels, predictions) * mask
        else:
            per_example_loss = self.mae_action_loss(labels, predictions) * mask

        per_example_loss = tf.reduce_sum(per_example_loss, axis=1) / seq_lens  # take mean along the timestep
        return tf.nn.compute_average_loss(per_example_loss, global_batch_size=self.global_batch_size)

    def compute_MAE(self, labels, predictions, mask, seq_lens, weightings=None):
        per_example_loss = self.mae_action_loss(labels, predictions) * mask  # B,T,D
        per_example_loss = tf.reduce_sum(per_example_loss, axis=1) / seq_lens  # take mean along the timestep -> B,D
        return tf.nn.compute_average_loss(per_example_loss, global_batch_size=self.global_batch_size)

    def compute_regularisation_loss(self, plan, encoding):
        # Reverse KL(enc|plan): we want planner to map to encoder (weighted by encoder)
        reg_loss = tfd.kl_divergence(encoding, plan)  # + KL(plan, encoding)
        return tf.nn.compute_average_loss(reg_loss, global_batch_size=self.global_batch_size)

    @staticmethod
    def compute_fp16_grads(optimizer, loss, tape, model):
        scaled_loss = optimizer.get_scaled_loss(loss)
        return tape.gradient(scaled_loss, model.trainable_variables)

    def make_sequences_variable_length(self, batch):
        '''
        This is a rather gross tiling/casting/indexing function - but it very effectively vectorises 
        variable sequence lengths over entire batches rather than in the dataloader, which brings us within 80% of precomputed speed
        while retaining full data aug!
        '''

        B = batch['obs'].shape[0]
        # Create a variable seq lens tensor
        seq_lens = tf.random.uniform(shape=[B], minval=self.args.window_size_min,
                                     maxval=self.args.window_size_max, dtype=tf.int32)
        batch['seq_lens'] = tf.cast(seq_lens, tf.float32)  # must be a float for later loss per timestep calcs
        # Create a mask which is length variable seq lens 
        mask = tf.cast(tf.sequence_mask(seq_lens, maxlen=self.args.window_size_max), tf.float32)  # B,T mask
        multiply_mask = tf.expand_dims(mask, -1)  # B, T, 1 (for broadcasting)

        batch['masks'] = mask
        batch['obs'] *= multiply_mask
        batch['acts'] *= multiply_mask
        # Save goals for later as it depends on whether it is imgs or not
        # Get numbers 0>B to concat with the seq lens for gather_nd
        B_range = tf.range(0, B, delta=1, dtype=tf.int32, name='range')  # B
        B_indices = tf.stack([B_range, seq_lens], axis=1)  # B,2
        if self.args.images:
            # get the goals corresponding to each of the seq len ends
            batch['goal_imgs'] = tf.gather_nd(batch['imgs'], B_indices)  # B, imgheight, imgwidth, 3
            # tile_dims = tf.constant([1, self.args.window_size_max, 1,1,1], tf.int32) 
            # goals = tf.tile(goals, tile_dims) # B, T, imgheight, imgwidth, 3
            imgs_mask = tf.cast(multiply_mask, tf.uint8)[:, :, :, tf.newaxis,
                        tf.newaxis]  # Must be 5 dim because the imgs are B, T, H, W, C
            # batch['goal_imgs'] = goals *imgs_mask
            # End goal specific stuff, start img specific stuff
            batch['imgs'] *= imgs_mask  # must be cast as int or this will be SLOW as it converts img to float
            batch['proprioceptive_features'] *= multiply_mask

            if self.args.gripper_images:
                batch[
                    'gripper_imgs'] *= imgs_mask  # must be cast as int or this will be SLOW as it converts img to float
            if self.args.images2:
                batch['imgs2'] *= imgs_mask
        else:

            goals = tf.gather_nd(batch['goals'], B_indices)[:, tf.newaxis, :]  # B, 1, achieved_goal_dim
            tile_dims = tf.constant([1, self.args.window_size_max, 1], tf.int32)
            goals = tf.tile(goals, tile_dims)  # B, T, achieved_goal_dim
            batch['goals'] = goals * multiply_mask  # B, T, achieved_goal_dim

        return batch

    # def gumbel_softmax_quantise(self, to_encode, states, goals):
    #     B,T,D = to_encode.shape
    #     to_encode = tf.reshape(to_encode, [B*self.args.vq_tiles, T//self.args.vq_tiles, D])#  [B*TILES, LEN/TILES, EMBEDDING]

    #     encoding = self.encoder([to_encode]) # [B*TILES, LATENT] 
    #     encoding = tf.reshape(encoding[:, :, tf.newaxis], [B, self.args.vq_tiles, -1]) # B, N_TILES, LATENT - so that each tile goes through the gumbel

    #     z_q = tfpl.DistributionLambda(
    #             lambda logits: tfd.RelaxedOneHotCategorical(self.args.temperature, encoding)
    #         )(encoding)
    #     z_hard = tf.math.argmax(encoding, axis=-1)  # [B, N_TILES, LATENT]

    #     z_hard = tf.one_hot(z_hard, encoding.shape[-1], dtype=z_q.dtype)  # [B, N_TILES, LATENT]

    #     z_enc = z_q + tf.stop_gradient(z_hard - z_q)
    #     z_enc = tf.reshape(z_enc, [B, self.args.vq_tiles, -1]) # Get it back down to batch, Tiles*Encoding where each _ is a tile but in one concatted vector now
    #     z_enc_tiled = tf.repeat(z_enc, repeats = T//self.args.vq_tiles, axis= 1) # tile out the discrete plans so the first 1 corresponds to the first N steps etc

    #     ## Planner
    #     # Tile out start and goal as many times as there are vqtiles - TODO maybe better to use positional encoding and a static planner?
    #     start_vq_tiled, goal_vq_tiled = tf.tile(states[:,0,:][:,tf.newaxis,:], [1, self.args.vq_tiles, 1]), tf.tile(goals[:,0,:][:,tf.newaxis,:], [1, self.args.vq_tiles, 1])
    #     plan = self.planner([start_vq_tiled, goal_vq_tiled]) # B, VQ_TILES, D

    #     z_plan_hard =  tf.one_hot(tf.math.argmax(plan, axis=-1), plan.shape[-1], dtype=plan.dtype)  # B, VQ_TILES, D

    #     z_plan_tiled = tf.repeat(z_plan_hard, repeats = T//self.args.vq_tiles, axis= 1)  # B,T,D

    #     return z_enc_tiled, z_plan_tiled

    def step(self, inputs=None, lang_labelled_inputs=None,
             external_videos=None):  # one of these must not be none, but either can be
        '''
        A function which wraps the shared processing between train and test step
        Maximally vectorises everything for speed's sake at the cost of some readability

        inputs is normal inputs without sentence labels : obs, acts, goals, imgs, proprioceptive_features, goal_imgs, mask

        '''

        indices = {'unlabelled': 0, 'labelled': 0, 'vids': 0}

        states, actions, goals, imgs, proprioceptive_features, goal_imgs, masks, sentence_embeddings = None, None, None, None, None, None, None, None

        if inputs is not None:
            states, actions, goals, seq_lens, masks = inputs['obs'], inputs['acts'], inputs['goals'], inputs[
                'seq_lens'], inputs['masks']
            indices['unlabelled'] = len(actions)
        # We want to concatenate the normal batch, lang_labelled_batch, and external videos, so that we can pass them all through 
        # the encoder at once, at the end, we'd like to return the langlabelled encodings(i.e encodings of lang labelled inputs and 
        # external videos), paired with their appropriate labels so that we can use contrastive losses on them 

        # Get the lengths of each, store that in a dictionary of indices 

        # if images, concat imgs, prorpioceptive_features, goal_imgs, not gripper_imgs - that can only be used where we have action labels

        # 1. When using imagesChange the definition of obs_dim to feature encoder dim + proprioceptive features
        # 2. Reshape imgs to B*T H W C.
        # 3. Sub in for states and goals.
        # 4. Then there should be no further changes!
        if self.args.images:

            if inputs is not None:
                # [B_unlab,T,H,W,C], [B_unlab,T,D], [B_unlab,H,W,C]
                imgs, proprioceptive_features, goal_imgs = inputs['imgs'], inputs['proprioceptive_features'], inputs[
                    'goal_imgs']

            # Frankly only care about these in the context of images
            if self.args.use_language and lang_labelled_inputs is not None:

                indices['labelled'] = indices['unlabelled'] + len(lang_labelled_inputs['acts'])
                if inputs is None:
                    imgs, proprioceptive_features, goal_imgs, actions, masks, seq_lens = lang_labelled_inputs['imgs'], \
                                                                                         lang_labelled_inputs[
                                                                                             'proprioceptive_features'], \
                                                                                         lang_labelled_inputs[
                                                                                             'goal_imgs'], \
                                                                                         lang_labelled_inputs['acts'], \
                                                                                         lang_labelled_inputs[
                                                                                             'masks'], tf.cast(
                        lang_labelled_inputs['seq_lens'], tf.float32)
                else:
                    #  [B_unlab + Blab, T, H, W, C],  [B_unlab + Blab, T, D], [B_unlab + Blab, H, W, C], [B_unlab + Blab, T, D]
                    imgs, proprioceptive_features, goal_imgs, actions, masks, seq_lens = tf.concat(
                        [imgs, lang_labelled_inputs['imgs']], 0), \
                                                                                         tf.concat(
                                                                                             [proprioceptive_features,
                                                                                              lang_labelled_inputs[
                                                                                                  'proprioceptive_features']],
                                                                                             0), \
                                                                                         tf.concat([goal_imgs,
                                                                                                    lang_labelled_inputs[
                                                                                                        'goal_imgs']],
                                                                                                   0), \
                                                                                         tf.concat([actions,
                                                                                                    lang_labelled_inputs[
                                                                                                        'acts']], 0), \
                                                                                         tf.concat([masks,
                                                                                                    lang_labelled_inputs[
                                                                                                        'masks']], 0), \
                                                                                         tf.concat([seq_lens, tf.cast(
                                                                                             lang_labelled_inputs[
                                                                                                 'seq_lens'],
                                                                                             tf.float32)], 0)

                sentence_embeddings = lang_labelled_inputs['label_embeddings']

                # contrastive only makes sense in the language context
                if self.args.use_contrastive and external_videos is not None:
                    indices['vids'] = indices['labelled'] + len(external_videos['imgs'])
                    # B_i +Bll, T, H, W, C
                    imgs, goal_imgs, masks = tf.concat([imgs, external_videos['imgs']], 0), tf.concat(
                        [goal_imgs, external_videos['goal_imgs']], 0), tf.concat([masks, external_videos['masks']],
                                                                                 0)  # don't need seq lens from these as it is only used on action level loss
                    sentence_embeddings = tf.concat([sentence_embeddings, external_videos['label_embeddings']], 0)

                # project the sentence embeddings to the same space as the goal
                # B_lab, D
                goal_sentence_embeddings = self.lang_embed_to_goal_space(sentence_embeddings)

            B, T, H, W, C = imgs.shape
            imgs = tf.reshape(imgs, [B * T, H, W, C])
            img_embeddings = tf.reshape(self.cnn(imgs)[0], [B, T, -1])  # [B,T,D]
            states = tf.concat([img_embeddings, proprioceptive_features],
                               -1)  # gets both the image and it's own xyz ori and angle as pose

            goal_img_embeddings = self.cnn(goal_imgs)[0]  # [B,D]
            img_in_goal_space = self.img_embed_to_goal_space(goal_img_embeddings)  # B, D

            # At this point, we have B_unlab+B_lab+B_vid goal image embeddings, and B_lab+B_vid sentence goal embeddings - all in the goal space!
            # Some fraction of our sentence labelled data should use the image data
            # as we don't have amy sentence labels here, just take all img embeddings
            unlabelled_goal_embeddings = img_in_goal_space[:indices['unlabelled']]
            # we want some images, some sentence embeddings
            if self.args.use_language and lang_labelled_inputs is not None:
                # 0 ..[unlabelled data].......... unlablled ...[image fraction of lang labelled data].... image_fraction .......[lang labelled data].......labelled .................[video data].....................vids
                image_fraction = int(indices['unlabelled'] + (
                        (indices['labelled'] - indices['unlabelled']) * self.args.sub_out_language_percent))
                lang_use_img_embeddings = img_in_goal_space[indices['unlabelled']:image_fraction]
                lang_use_lang_embeddings = goal_sentence_embeddings[len(lang_use_img_embeddings):indices['labelled']]
                labelled_goal_embeddings = tf.concat([lang_use_img_embeddings, lang_use_lang_embeddings], 0)
                goals = tf.concat([unlabelled_goal_embeddings, labelled_goal_embeddings], 0)  # Bunlab + Blab, D
                # Same for the vids
                if self.args.use_contrastive:
                    image_fraction = int(indices['unlabelled'] + (
                            (indices['vids'] - indices['unlabelled']) * self.args.sub_out_language_percent))
                    vids_use_img_embeddings = img_in_goal_space[indices['labelled']:image_fraction]
                    vids_use_lang_embeddings = goal_sentence_embeddings[len(labelled_goal_embeddings):]
                    video_goal_embeddings = tf.concat([vids_use_img_embeddings, vids_use_lang_embeddings], 0)
                    goals = tf.concat([goals, video_goal_embeddings], 0)
                # B,1,embedsize

            else:
                goals = unlabelled_goal_embeddings
            # The above two will just be 0 if there is nothing from those batches
            if self.args.gripper_images:
                if inputs is not None:
                    gripper_imgs = inputs['gripper_imgs']
                if self.args.use_language and lang_labelled_inputs is not None:
                    if inputs is None:  # Get rid of this if else hell later TODO
                        gripper_imgs = lang_labelled_inputs['gripper_imgs']
                    else:
                        gripper_imgs = tf.concat([gripper_imgs, lang_labelled_inputs['gripper_imgs']], 0)

                B_grip, _, H_g, W_g, C = gripper_imgs.shape
                gripper_imgs = tf.reshape(gripper_imgs, [B * T, H_g, W_g, C])
                gripper_embeddings = tf.reshape(self.gripper_cnn(gripper_imgs)[0],
                                                [B, T, -1])  # should be [B, T, args.gripper_img_embedding_size]
                states = tf.concat([states, gripper_embeddings], -1)
            if self.args.images2:
                if inputs is not None:
                    imgs2 = inputs['imgs2']
                if self.args.use_language and lang_labelled_inputs is not None:
                    if inputs is None:  # Get rid of this if else hell later TODO
                        imgs2 = lang_labelled_inputs['imgs2']
                    else:
                        imgs2 = tf.concat([imgs2, lang_labelled_inputs['imgs2']], 0)

                B_grip, _, H_g, W_g, C = imgs2.shape
                imgs2 = tf.reshape(imgs2, [B * T, H_g, W_g, C])
                imgs2_embedding = tf.reshape(self.cnn(imgs2)[0],
                                             [B, T, -1])  # should be [B, T, args.img_embedding_size]
                states = tf.concat([states, imgs2_embedding], -1)

            # B, T, D
            goals = tf.tile(goals[:, tf.newaxis, :], [1, self.args.window_size_max, 1])
            goals = goals * masks[:, :, tf.newaxis]  # B, T, 1 (for broadcasting)

        if self.args.gcbc:
            distrib = self.actor([states, goals])
            return distrib
        else:
            if self.args.encode_all:
                to_encode = tf.concat([states, actions], -1)
            else:
                to_encode = img_embeddings

            if self.args.discrete:
                # We want to chunk up the inputs, so each seq goes from B, LEN, EMBED to 
                # that way the lstm encodes little chunks of the sequence

                # One option is to use gumbel, but for the moment that seems to be more unstable. 

                encoding = self.encoder([to_encode])  # [B, T, to_enc_dim]  > [B,VQ_tiles,latent_dim]
                B, tiles, latent = encoding.shape
                VQ_output = self.VQ(encoding)
                encoding = tf.reshape(VQ_output['quantised'], (B, tiles, latent))
                z_enc_tiled = tf.repeat(encoding, self.args.window_size_max // tiles, 1)  # B,T,D

                encoder_indices = tf.reshape(VQ_output['indices'], (B, -1))

                # NB: ATM THIS IS A LITTLE ANNOYING - MUST TAKE FROM N=1 BECAUSE WE HAVE TWO INPUTS BEFORE (GOAL AND STATE)
                plan_logits = self.planner((states[:, 0, :], goals[:, 0, :], encoder_indices[:, :-1]))['logits'][:,
                              1:]  # [B, tiles, n_codebook_logits]

                plan_indices = tf.argmax(tf.nn.softmax(plan_logits, -1), -1)  # B, tiles

                plan = tf.reshape(self.VQ.quantise(plan_indices), (B, tiles, latent))  # [B, tiles, latent]

                VQ_output['plan_logits'] = plan_logits
                VQ_output['plan_codebook_indices'] = plan_indices
                VQ_output['encoder_indices'] = encoder_indices

                z_plan_tiled = tf.repeat(plan, self.args.window_size_max // tiles, 1)  # B,T,D


            else:
                plan = self.planner([states[:, 0, :], goals[:, 0,
                                                      :]])  # the final goals are tiled out over the entire non masked sequence, so the first timestep is the final goal.
                encoding = self.encoder([to_encode])
                z_enc = encoding.sample()
                z_plan = plan.sample()
                z_enc_tiled = tf.tile(tf.expand_dims(z_enc, 1), (1, self.dl.window_size, 1))
                z_plan_tiled = tf.tile(tf.expand_dims(z_plan, 1), (1, self.dl.window_size, 1))
                VQ_output = None

            enc_policy = self.actor([states, z_enc_tiled, goals])
            plan_policy = self.actor([states, z_plan_tiled, goals])
            return {'enc_policy': enc_policy, 'plan_policy': plan_policy, 'encoding': encoding, 'plan': plan,
                    'indices': indices,
                    'actions': actions, 'masks': masks, 'seq_lens': seq_lens,
                    'sentence_embeddings': sentence_embeddings, 'VQ': VQ_output}
            # How is step used?
            # In the train and test functions below, where we use enc policy for logloss, plan policy for validation mae, encoding and plan for reg loss
            # Additionally, for contrastive loss we need to get the encodings of the lang labelled and vids only, and their sentence embeddings
            # Then maybe randomly offset by one - layout vid lab vid, or lab vid lab? Then take positive pairs/negative pairs via distance. Ez!
            # Plot just needs the first four...? Meh it can do that itself.
            # In the event that we did contrastive, we get out encodings which will be [b_lab + b_lab + b_vid, D] and the plans which will chase them. 
            # We'll also get indices, unlab, lab, vid
            # therefore, to do contrastive all we need to do is chop off the first B_unlab worth, then the encodings and the sentrence embeddings will be the same length

    def train_step(self, **kwargs):
        inputs, beta, lang_labelled_inputs, external_videos, bulk = kwargs['batch'], kwargs['beta'], kwargs['lang'], \
                                                                    kwargs['video'], kwargs['bulk']

        if self.args.bulk_split > 0:
            inputs = {k: tf.concat([inputs[k], bulk[k]], axis=0) for k in inputs.keys()}  # combine them

        inputs = self.make_sequences_variable_length(inputs)

        with tf.GradientTape() as actor_tape, tf.GradientTape() as encoder_tape, tf.GradientTape() as planner_tape, tf.GradientTape() as cnn_tape, tf.GradientTape() as gripper_cnn_tape, \
                tf.GradientTape() as img_goal_embed_tape, tf.GradientTape() as lang_goal_embed_tape, tf.GradientTape() as VQ_tape:

            if self.args.gcbc:
                policy = self.step(inputs)
                loss = self.compute_loss(actions, policy, mask, seq_lens)
                gradients = actor_tape.gradient(loss, self.actor.trainable_variables)
                self.actor_optimizer.apply_gradients(zip(gradients, self.actor.trainable_variables))
            else:
                step = self.step(inputs, lang_labelled_inputs, external_videos)
                enc_policy, plan_policy, encoding, plan, actions, mask, seq_lens = step['enc_policy'], step[
                    'plan_policy'], step['encoding'], step['plan'], step['actions'], step['masks'], step['seq_lens']
                act_enc_loss = record(self.compute_loss(actions, enc_policy, mask, seq_lens),
                                      self.metrics['train_act_with_enc_loss'])
                if self.args.discrete:
                    # planner_loss = tf.nn.softmax_cross_entropy_with_logits(labels = tf.stop_gradient(tf.nn.softmax(encoding,-1)), logits=plan)
                    # record(planner_loss, self.metrics['train_discrete_planner_loss'])
                    record(lfp.VQ.entropy(step['VQ']['entropy']), self.metrics['entropy'])

                    loss = act_enc_loss + record(step['VQ']['commitment_loss'], self.metrics['commitment_loss'])

                    planner_decisions = tf.argmax(tf.nn.softmax(step['VQ']['plan_logits'], -1), -1)
                    planner_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(
                        labels=tf.stop_gradient(step['VQ']['encoder_indices']), logits=step['VQ']['plan_logits']))
                    train_plan_acc = record(
                        tf.reduce_mean(tf.cast(planner_decisions == step['VQ']['encoder_indices'], tf.float32)),
                        self.metrics['train_discrete_planner_acc'])
                    loss += record(planner_loss, self.metrics['train_discrete_planner_loss'])
                else:
                    act_plan_loss = record(self.compute_loss(actions, plan_policy, mask, seq_lens),
                                           self.metrics['train_act_with_plan_loss'])
                    reg_loss = record(self.compute_regularisation_loss(plan, encoding), self.metrics['train_reg_loss'])
                    loss = act_enc_loss + reg_loss * beta

                if self.args.fp16:
                    actor_gradients = self.compute_fp16_grads(self.actor_optimizer, loss, actor_tape, self.actor)
                    encoder_gradients = self.compute_fp16_grads(self.encoder_optimizer, loss, encoder_tape,
                                                                self.encoder)
                    planner_gradients = self.compute_fp16_grads(self.planner_optimizer, loss, planner_tape,
                                                                self.planner)
                    if self.args.images:
                        cnn_gradients = self.compute_fp16_grads(self.cnn_optimizer, loss, cnn_tape, self.cnn)
                        goal_to_goal_space_grads = self.compute_fp16_grads(self.img_embed_to_goal_space_optimizer, loss,
                                                                           img_goal_embed_tape,
                                                                           self.img_embed_to_goal_space)
                    if self.args.gripper_images: gripper_cnn_gradients = self.compute_fp16_grads(
                        self.gripper_cnn_optimizer, loss, gripper_cnn_tape, self.gripper_cnn)
                    if self.args.use_language: raise NotImplementedError
                else:
                    actor_gradients = actor_tape.gradient(loss, self.actor.trainable_variables)
                    encoder_gradients = encoder_tape.gradient(loss, self.encoder.trainable_variables)
                    planner_gradients = planner_tape.gradient(loss, self.planner.trainable_variables)
                    if self.args.images:
                        cnn_gradients = cnn_tape.gradient(loss, self.cnn.trainable_variables)
                        img_goal_to_goal_space_grads = img_goal_embed_tape.gradient(loss,
                                                                                    self.img_embed_to_goal_space.trainable_variables)
                    if self.args.gripper_images: gripper_cnn_gradients = gripper_cnn_tape.gradient(loss,
                                                                                                   self.gripper_cnn.trainable_variables)
                    if self.args.use_language: lang_goal_to_goal_space_grads = lang_goal_embed_tape.gradient(loss,
                                                                                                             self.lang_embed_to_goal_space.trainable_variables)
                    if self.args.discrete and not self.args.vq_ema: VQ_gradients = VQ_tape.gradient(loss,
                                                                                                    [self.VQ.codebook])

                #################### Calc indivual norms
                actor_norm = record(tf.linalg.global_norm(actor_gradients), self.metrics['actor_grad_norm'])
                encoder_norm = record(tf.linalg.global_norm(encoder_gradients), self.metrics['encoder_grad_norm'])
                planner_norm = record(tf.linalg.global_norm(planner_gradients), self.metrics['planner_grad_norm'])
                if self.args.images:
                    cnn_norm = record(tf.linalg.global_norm(cnn_gradients), self.metrics['cnn_grad_norm'])
                    img_goal_to_goal_space_norm = record(tf.linalg.global_norm(img_goal_to_goal_space_grads),
                                                         self.metrics['img_embed_to_goal_space_norm'])
                if self.args.gripper_images: gripper_cnn_norm = record(tf.linalg.global_norm(gripper_cnn_gradients),
                                                                       self.metrics['gripper_cnn_grad_norm'])
                if self.args.use_language: lang_goal_to_goal_space_norm = record(
                    tf.linalg.global_norm(lang_goal_to_goal_space_grads), self.metrics['lang_embed_to_goal_space_norm'])
                if self.args.discrete and not self.args.vq_ema:  record(tf.linalg.global_norm(VQ_gradients),
                                                                        self.metrics['VQ_grad_norm'])
                ##################### Calc global grad norm
                gradients = actor_gradients + encoder_gradients + planner_gradients
                if self.args.images: gradients = gradients + cnn_gradients + img_goal_to_goal_space_grads
                if self.args.gripper_images: gradients += gripper_cnn_gradients
                if self.args.use_language: gradients += lang_goal_to_goal_space_grads
                record(tf.linalg.global_norm(gradients), self.metrics['global_grad_norm'])

                #################### Apply optimizer updates
                self.actor_optimizer.apply_gradients(zip(actor_gradients, self.actor.trainable_variables))
                self.encoder_optimizer.apply_gradients(zip(encoder_gradients, self.encoder.trainable_variables))
                self.planner_optimizer.apply_gradients(zip(planner_gradients, self.planner.trainable_variables))
                if self.args.images:
                    self.cnn_optimizer.apply_gradients(zip(cnn_gradients, self.cnn.trainable_variables))
                    self.img_embed_to_goal_space_optimizer.apply_gradients(
                        zip(img_goal_to_goal_space_grads, self.img_embed_to_goal_space.trainable_variables))
                if self.args.gripper_images: self.gripper_cnn_optimizer.apply_gradients(
                    zip(gripper_cnn_gradients, self.gripper_cnn.trainable_variables))
                if self.args.use_language: self.lang_embed_to_goal_space_optimizer.apply_gradients(
                    zip(lang_goal_to_goal_space_grads, self.lang_embed_to_goal_space.trainable_variables))
                if self.args.discrete and not self.args.vq_ema:   self.VQ_optimizer.apply_gradients(
                    zip(VQ_gradients, [self.VQ.codebook]))
                ################### Fin

        return_dict = {'loss': record(loss, self.metrics['train_loss'])}
        if self.args.discrete and self.args.vq_ema:
            return_dict['flattened_inputs'] = step['VQ']['flattened_inputs']
            return_dict['encodings'] = step['VQ']['encodings']

        return return_dict

    def test_step(self, **kwargs):
        inputs, beta, lang_labelled_inputs, external_videos = kwargs['batch'], kwargs['beta'], kwargs['lang'], kwargs[
            'video']

        inputs = self.make_sequences_variable_length(inputs)  #
        actions, seq_lens, mask = inputs['acts'], inputs['seq_lens'], inputs['masks']

        if self.args.gcbc:
            policy = self.step(inputs)
            loss = self.compute_loss(actions, policy, mask, seq_lens)
            log_action_breakdown(policy, actions, mask, seq_lens, self.args.num_distribs is not None,
                                 self.dl.quaternion_act, self.valid_position_loss, self.valid_max_position_loss, \
                                 self.valid_rotation_loss, self.valid_max_rotation_loss, self.valid_gripper_loss,
                                 self.compute_MAE)
        else:
            step = self.step(inputs, lang_labelled_inputs, external_videos)
            enc_policy, plan_policy, encoding, plan, actions, mask, seq_lens, indices = step['enc_policy'], step[
                'plan_policy'], step['encoding'], step['plan'], step['actions'], step['masks'], step['seq_lens'], step[
                                                                                            'indices']

            act_enc_loss = record(self.compute_loss(actions, enc_policy, mask, seq_lens),
                                  self.metrics['valid_act_with_enc_loss'])

            if self.args.discrete:
                record(lfp.VQ.entropy(step['VQ']['entropy']), self.metrics['entropy'])
                loss = act_enc_loss + record(step['VQ']['commitment_loss'], self.metrics['commitment_loss'])

                planner_decisions = tf.argmax(tf.nn.softmax(step['VQ']['plan_logits'], -1), -1)
                planner_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(
                    labels=tf.stop_gradient(step['VQ']['encoder_indices']), logits=step['VQ']['plan_logits']))
                valid_plan_acc = record(
                    tf.reduce_mean(tf.cast(planner_decisions == step['VQ']['encoder_indices'], tf.float32)),
                    self.metrics['valid_discrete_planner_acc'])
                loss += record(planner_loss, self.metrics['valid_discrete_planner_loss'])
            else:
                act_plan_loss = record(self.compute_loss(actions, plan_policy, mask, seq_lens),
                                       self.metrics['valid_act_with_plan_loss'])
                reg_loss = record(self.compute_regularisation_loss(plan, encoding), self.metrics['valid_reg_loss'])
                loss = act_plan_loss + reg_loss * beta
            log_action_breakdown(plan_policy, actions, mask, seq_lens, self.args.num_distribs is not None,
                                 self.dl.quaternion_act, self.metrics['valid_position_loss'],
                                 self.metrics['valid_max_position_loss'], self.metrics['valid_rotation_loss'],
                                 self.metrics['valid_max_rotation_loss'], self.metrics['valid_gripper_loss'],
                                 self.compute_MAE)
            log_action_breakdown(enc_policy, actions, mask, seq_lens, self.args.num_distribs is not None,
                                 self.dl.quaternion_act, self.metrics['valid_enc_position_loss'],
                                 self.metrics['valid_enc_max_position_loss'], self.metrics['valid_enc_rotation_loss'],
                                 self.metrics['valid_enc_max_rotation_loss'], self.metrics['valid_enc_gripper_loss'],
                                 self.compute_MAE)

            if self.args.use_language:
                # setting probabilistic = false and just passing in the .sample() of the distrib as for some reason slicing it auto samples?
                log_action_breakdown(plan_policy.sample()[indices['unlabelled']:], actions[indices['unlabelled']:],
                                     mask[indices['unlabelled']:], seq_lens[indices['unlabelled']:], False,
                                     self.dl.quaternion_act,
                                     self.metrics['valid_lang_position_loss'],
                                     self.metrics['valid_lang_max_position_loss'],
                                     self.metrics['valid_lang_rotation_loss'],
                                     self.metrics['valid_lang_max_rotation_loss'],
                                     self.metrics['valid_lang_gripper_loss'], self.compute_MAE)

        return {'loss': record(loss, self.metrics['valid_loss'])}

    @tf.function
    def distributed_train_step(self, inputs):
        per_replica_losses = self.strategy.run(self.train_step, kwargs=inputs)

        return self.strategy.reduce(tf.distribute.ReduceOp.MEAN, per_replica_losses, axis=None)

    @tf.function
    def distributed_test_step(self, inputs):
        per_replica_losses = self.strategy.run(self.test_step, kwargs=inputs)
        return self.strategy.reduce(tf.distribute.ReduceOp.MEAN, per_replica_losses, axis=None)

    def get_saved_objects(self):
        saved_objects = {'actor': self.actor,
                         'encoder': self.encoder,
                         'planner': self.planner,
                         'actor_optimizer': self.actor_optimizer,
                         'encoder_optimizer': self.encoder_optimizer,
                         'planner_optimizer': self.planner_optimizer}
        if self.args.images: saved_objects['cnn'], saved_objects['cnn_optimizer'], saved_objects['img_goal_embed'], \
                             saved_objects[
                                 'img_goal_embed_optimizer'] = self.cnn, self.cnn_optimizer, self.img_embed_to_goal_space, self.lang_embed_to_goal_space_optimizer
        if self.args.gripper_images: saved_objects['gripper_cnn'], saved_objects[
            'griper_cnn_optimizer'] = self.gripper_cnn, self.gripper_cnn_optimizer
        if self.args.use_language: saved_objects['lang_goal_embed'], saved_objects[
            'lang_goal_embed_optimizer'] = self.lang_embed_to_goal_space, self.lang_embed_to_goal_space_optimizer
        if self.args.discrete: saved_objects['VQ'] = self.VQ
        return saved_objects

    def save_weights(self, path, run_id=None, experiment_key=None):

        if self.chkpt_manager is None:
            ckpt = tf.train.Checkpoint(**self.get_saved_objects())
            self.chkpt_manager = tf.train.CheckpointManager(ckpt, path, max_to_keep=1)
            save_path = self.chkpt_manager.save()
        else:
            save_path = self.chkpt_manager.save()

    def load_weights(self, path, with_optimizer=False, from_checkpoint=False):
        # With checkpoint
        if os.path.exists(path):
            ckpt = tf.train.Checkpoint(**self.get_saved_objects())
            self.chkpt_manager = tf.train.CheckpointManager(ckpt, path, max_to_keep=1)
            ckpt.restore(tf.train.latest_checkpoint(path))
        else:
            print(f"try: gsutil -m cp -r gs://{self.args.bucket_name}/saved_models/{self.args.run_name} saved_models/")


def train_setup(args, dl, GLOBAL_BATCH_SIZE, strategy):
    model_params = {'obs_dim': args.img_embedding_size + dl.proprioceptive_features_dim if args.images else dl.obs_dim,
                    'goal_dim': args.goal_space_dim if args.images else dl.goal_dim,
                    'act_dim': dl.act_dim,
                    'layer_size': args.actor_layer_size,
                    'latent_dim': args.latent_dim}

    if args.gripper_images:  # separate this from args.images because pybullet sim doens't have a gripper cam in the collected data
        model_params['obs_dim'] += args.gripper_img_embedding_size
    if args.images2:  # separate this from args.images because pybullet sim doens't have a gripper cam in the collected data
        model_params['obs_dim'] += args.img_embedding_size

    if args.gcbc:
        encoder = None
        planner = None
    else:
        model_params['layer_size'] = args.encoder_layer_size
        if args.encode_all:
            model_params['enc_in_dim'] = model_params['obs_dim'] + model_params['act_dim']
        else:
            model_params['enc_in_dim'] = args.img_embedding_size
        model_params['layer_size'] = args.planner_layer_size
        if args.discrete:
            planner = lfp.model.conditional_transformer(args.codebook_size, max_len=args.window_size_max + 2,
                                                        embed_dim=args.latent_dim, num_heads=8,
                                                        feed_forward_dim=args.planner_layer_size, num_layers=6)
            model_params['reductions'] = args.vq_reduction
            encoder = lfp.model.create_discrete_encoder(**model_params)
        else:
            encoder = lfp.model.create_encoder(**model_params)
            planner = lfp.model.create_planner(**model_params)

    model_params['layer_size'] = args.actor_layer_size
    actor = lfp.model.create_actor(**model_params, gcbc=args.gcbc, num_distribs=args.num_distribs, qbits=args.qbits,
                                   discrete=args.discrete)

    cnn, gripper_cnn, img_embed_to_goal_space, lang_embed_to_goal_space = None, None, None, None
    if args.images:
        cnn = lfp.model.CNN_DICT[args.cnn_type](dl.img_size, dl.img_size, embedding_size=args.img_embedding_size)
        lfp.utils.build_cnn(
            cnn)  # Have to do this because it is subclassed and the reshapes in the spatial softmax don't play nice with model auto build
        if args.gripper_images:  # gripper cnn is always impala style, it moves around too much
            gripper_cnn = lfp.model.CNN_DICT['impala'](dl.gripper_img_size, dl.gripper_img_size,
                                                       embedding_size=args.gripper_img_embedding_size)
            lfp.utils.build_cnn(
                gripper_cnn)  # Have to do this becasue it is subclassed and the reshapes in the spatial softmax don't play nice with model auto build
        img_embed_to_goal_space = lfp.model.create_goal_space_mapper(args.img_embedding_size, args.goal_space_dim,
                                                                     args.goal_mapper_layer_size)
        if args.use_language:
            lang_embed_to_goal_space = lfp.model.create_goal_space_mapper(args.sentence_embedding_size,
                                                                          args.goal_space_dim,
                                                                          args.goal_mapper_layer_size)

    # optimizer = tfa.optimizers.LAMB(learning_rate=args.learning_rate)
    optimizer = adam_v2.Adam
    trainer = LFPTrainer(args, actor, dl, encoder, planner, cnn, gripper_cnn, img_embed_to_goal_space,
                         lang_embed_to_goal_space, optimizer, strategy, GLOBAL_BATCH_SIZE)
    return actor, encoder, planner, cnn, gripper_cnn, img_embed_to_goal_space, lang_embed_to_goal_space, trainer
