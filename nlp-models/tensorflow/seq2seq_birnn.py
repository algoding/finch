import tensorflow as tf
from tensorflow.python.layers.core import Dense
import numpy as np


class Seq2Seq:
    def __init__(self, rnn_size, n_layers,
                 X_word2idx, encoder_embedding_dim,
                 Y_word2idx, decoder_embedding_dim,
                 batch_size, sess=tf.Session(), grad_clip=5.0):
        self.rnn_size = rnn_size
        self.n_layers  = n_layers
        self.grad_clip = grad_clip
        self.X_word2idx = X_word2idx
        self.encoder_embedding_dim = encoder_embedding_dim
        self.Y_word2idx = Y_word2idx
        self.decoder_embedding_dim = decoder_embedding_dim
        self.batch_size = batch_size
        self.sess = sess

        self.register_symbols()
        self.build_graph()
    # end constructor


    def register_symbols(self):
        self._x_go = self.X_word2idx['<GO>']
        self._x_eos = self.X_word2idx['<EOS>']
        self._x_pad = self.X_word2idx['<PAD>']
        self._x_unk = self.X_word2idx['<UNK>']

        self._y_go = self.Y_word2idx['<GO>']
        self._y_eos = self.Y_word2idx['<EOS>']
        self._y_pad = self.Y_word2idx['<PAD>']
        self._y_unk = self.Y_word2idx['<UNK>']
    # end method add_symbols


    def build_graph(self):
        self.add_input_layer()
        self.add_encoder_layer()
        self.add_decoder_layer()
        self.add_backward_path()
    # end method build_graph


    def add_input_layer(self):
        self.X = tf.placeholder(tf.int32, [self.batch_size, None])
        self.Y = tf.placeholder(tf.int32, [self.batch_size, None])
        self.X_seq_len = tf.placeholder(tf.int32, [self.batch_size])
        self.Y_seq_len = tf.placeholder(tf.int32, [self.batch_size])
    # end method add_input_layer


    def lstm_cell(self):
        return tf.nn.rnn_cell.LSTMCell(self.rnn_size, initializer=tf.orthogonal_initializer())
    # end method lstm_cell


    def add_encoder_layer(self):
        birnn_out = tf.contrib.layers.embed_sequence(self.X, len(self.X_word2idx), self.encoder_embedding_dim)
        self.encoder_state = ()
        for n in range(self.n_layers):
            (out_fw, out_bw), (state_fw, state_bw) = tf.nn.bidirectional_dynamic_rnn(
                cell_fw = self.lstm_cell(), cell_bw = self.lstm_cell(),
                inputs = birnn_out,
                sequence_length = self.X_seq_len,
                dtype = tf.float32,
                scope = 'bidirectional_rnn_'+str(n))
            birnn_out = tf.concat((out_fw, out_bw), 2)
            self.encoder_state += (state_fw, state_bw)
    # end method add_encoder_layer
    

    def processed_decoder_input(self):
        main = tf.strided_slice(self.Y, [0, 0], [self.batch_size, -1], [1, 1]) # remove last char
        decoder_input = tf.concat([tf.fill([self.batch_size, 1], self._y_go), main], 1)
        return decoder_input
    # end method add_decoder_layer


    def prepare_decoder_components(self):
        self.decoder_cell = tf.nn.rnn_cell.MultiRNNCell([self.lstm_cell() for _ in range(2 * self.n_layers)])
        Y_vocab_size = len(self.Y_word2idx)
        self.decoder_embedding = tf.get_variable('decoder_embedding', [Y_vocab_size, self.decoder_embedding_dim],
                                                 tf.float32, tf.random_uniform_initializer(-1.0, 1.0))
        self.projection_layer = Dense(Y_vocab_size)
        self.X_seq_max_len = tf.reduce_max(self.X_seq_len)
        self.Y_seq_max_len = tf.reduce_max(self.Y_seq_len)
    # end method prepare_decoder_components


    def add_decoder_layer(self):
        self.prepare_decoder_components()

        training_helper = tf.contrib.seq2seq.TrainingHelper(
            inputs = tf.nn.embedding_lookup(self.decoder_embedding, self.processed_decoder_input()),
            sequence_length = self.Y_seq_len,
            time_major = False)
        training_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell = self.decoder_cell,
            helper = training_helper,
            initial_state = self.encoder_state,
            output_layer = self.projection_layer)
        training_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder = training_decoder,
            impute_finished = True,
            maximum_iterations = self.Y_seq_max_len)
        self.training_logits = training_decoder_output.rnn_output
        
        predicting_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
            embedding = self.decoder_embedding,
            start_tokens = tf.tile(tf.constant([self._y_go], dtype=tf.int32), [self.batch_size]),
            end_token = self._y_eos)
        predicting_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell = self.decoder_cell,
            helper = predicting_helper,
            initial_state = self.encoder_state,
            output_layer = self.projection_layer)
        predicting_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder = predicting_decoder,
            impute_finished = True,
            maximum_iterations = 2 * self.X_seq_max_len)
        self.predicting_logits = predicting_decoder_output.sample_id
    # end method add_decoder_layer


    def add_backward_path(self):
        masks = tf.sequence_mask(self.Y_seq_len, self.Y_seq_max_len, dtype=tf.float32)
        self.loss = tf.contrib.seq2seq.sequence_loss(logits = self.training_logits,
                                                     targets = self.Y,
                                                     weights = masks)
        # gradient clipping
        params = tf.trainable_variables()
        gradients = tf.gradients(self.loss, params)
        clipped_gradients, _ = tf.clip_by_global_norm(gradients, self.grad_clip)
        self.train_op = tf.train.AdamOptimizer().apply_gradients(zip(clipped_gradients, params))
    # end method add_backward_path


    def pad_sentence_batch(self, sentence_batch, pad_int):
        padded_seqs = []
        seq_lens = []
        max_sentence_len = max([len(sentence) for sentence in sentence_batch])
        for sentence in sentence_batch:
            padded_seqs.append(sentence + [pad_int] * (max_sentence_len - len(sentence)))
            seq_lens.append(len(sentence))
        return padded_seqs, seq_lens
    # end method pad_sentence_batch


    def next_batch(self, X, Y, X_pad_int=None, Y_pad_int=None):
        if X_pad_int is None:
            X_pad_int = self._x_pad
        if Y_pad_int is None:
            Y_pad_int = self._y_pad
        
        for i in range(0, len(X) - len(X) % self.batch_size, self.batch_size):
            X_batch = X[i : i + self.batch_size]
            Y_batch = Y[i : i + self.batch_size]
            padded_X_batch, X_batch_lens = self.pad_sentence_batch(X_batch, X_pad_int)
            padded_Y_batch, Y_batch_lens = self.pad_sentence_batch(Y_batch, Y_pad_int)
            yield (np.array(padded_X_batch),
                   np.array(padded_Y_batch),
                   X_batch_lens,
                   Y_batch_lens)
    # end method gen_batch


    def fit(self, X_train, Y_train, val_data, n_epoch=60, display_step=50):
        X_test, Y_test = val_data
        X_test_batch, Y_test_batch, X_test_batch_lens, Y_test_batch_lens = next(self.next_batch(X_test, Y_test))

        self.sess.run(tf.global_variables_initializer())
        for epoch in range(1, n_epoch+1):
            for local_step, (X_train_batch, Y_train_batch,
                             X_train_batch_lens, Y_train_batch_lens) in enumerate(self.next_batch(X_train,
                                                                                                  Y_train)):
                _, loss = self.sess.run([self.train_op, self.loss], {self.X: X_train_batch,
                                                                     self.Y: Y_train_batch,
                                                                     self.X_seq_len: X_train_batch_lens,
                                                                     self.Y_seq_len: Y_train_batch_lens})
                if local_step % display_step == 0:
                    val_loss = self.sess.run(self.loss, {self.X: X_test_batch,
                                                         self.Y: Y_test_batch,
                                                         self.X_seq_len: X_test_batch_lens,
                                                         self.Y_seq_len: Y_test_batch_lens})
                    print("Epoch %d/%d | Batch %d/%d | train_loss: %.3f | test_loss: %.3f"
                        % (epoch, n_epoch, local_step, len(X_train)//self.batch_size, loss, val_loss))
    # end method fit


    def infer(self, input_word, X_idx2word, Y_idx2word):        
        input_indices = [self.X_word2idx.get(char, self._x_unk) for char in input_word]
        out_indices = self.sess.run(self.predicting_logits, {
            self.X: [input_indices] * self.batch_size,
            self.X_seq_len: [len(input_indices)] * self.batch_size})[0]
        
        print('\nSource')
        print('Word: {}'.format([i for i in input_indices]))
        print('IN: {}'.format(' '.join([X_idx2word[i] for i in input_indices])))
        
        print('\nTarget')
        print('Word: {}'.format([i for i in out_indices]))
        print('OUT: {}'.format(' '.join([Y_idx2word[i] for i in out_indices])))
    # end method infer
# end class