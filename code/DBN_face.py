"""
"""
import os
import sys
import optparse
import time

import numpy

import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams

from logistic_sgd import LogisticRegression, load_data
from mlp import HiddenLayer
from rbm import RBM
from load_faces import load_faces

try:
    import PIL.Image as Image
except ImportError:
    import Image
from utils import tile_raster_images

import cPickle

# start-snippet-1
class DBN(object):
    """Deep Belief Network

    A deep belief network is obtained by stacking several RBMs on top of each
    other. The hidden layer of the RBM at layer `i` becomes the input of the
    RBM at layer `i+1`. The first layer RBM gets as input the input of the
    network, and the hidden layer of the last RBM represents the output. When
    used for classification, the DBN is treated as a MLP, by adding a logistic
    regression layer on top.
    """

    def __init__(self, numpy_rng, theano_rng=None, n_ins=784,
                 hidden_layers_sizes=[500, 500], n_outs=10):
        """This class is made to support a variable number of layers.

        :type numpy_rng: numpy.random.RandomState
        :param numpy_rng: numpy random number generator used to draw initial
                    weights

        :type theano_rng: theano.tensor.shared_randomstreams.RandomStreams
        :param theano_rng: Theano random generator; if None is given one is
                           generated based on a seed drawn from `rng`

        :type n_ins: int
        :param n_ins: dimension of the input to the DBN

        :type hidden_layers_sizes: list of ints
        :param hidden_layers_sizes: intermediate layers size, must contain
                               at least one value

        :type n_outs: int
        :param n_outs: dimension of the output of the network
        """

        self.sigmoid_layers = []
        self.rbm_layers = []
        self.params = []
        self.n_layers = len(hidden_layers_sizes)

        assert self.n_layers > 0

        if not theano_rng:
            theano_rng = RandomStreams(numpy_rng.randint(2 ** 30))

        # allocate symbolic variables for the data
        self.x = T.matrix('x')  # the data is presented as rasterized images
        self.y = T.ivector('y')  # the labels are presented as 1D vector
                                 # of [int] labels
        # end-snippet-1
        # The DBN is an MLP, for which all weights of intermediate
        # layers are shared with a different RBM.  We will first
        # construct the DBN as a deep multilayer perceptron, and when
        # constructing each sigmoidal layer we also construct an RBM
        # that shares weights with that layer. During pretraining we
        # will train these RBMs (which will lead to chainging the
        # weights of the MLP as well) During finetuning we will finish
        # training the DBN by doing stochastic gradient descent on the
        # MLP.

        for i in xrange(self.n_layers):
            # construct the sigmoidal layer

            # the size of the input is either the number of hidden
            # units of the layer below or the input size if we are on
            # the first layer
            if i == 0:
                input_size = n_ins
            else:
                input_size = hidden_layers_sizes[i - 1]

            # the input to this layer is either the activation of the
            # hidden layer below or the input of the DBN if you are on
            # the first layer
            if i == 0:
                layer_input = self.x
            else:
                layer_input = self.sigmoid_layers[-1].output

            sigmoid_layer = HiddenLayer(rng=numpy_rng,
                                        input=layer_input,
                                        n_in=input_size,
                                        n_out=hidden_layers_sizes[i],
                                        activation=T.nnet.sigmoid)

            # add the layer to our list of layers
            self.sigmoid_layers.append(sigmoid_layer)

            # its arguably a philosophical question...  but we are
            # going to only declare that the parameters of the
            # sigmoid_layers are parameters of the DBN. The visible
            # biases in the RBM are parameters of those RBMs, but not
            # of the DBN.
            self.params.extend(sigmoid_layer.params)

            # Construct an RBM that shared weights with this layer
            rbm_layer = RBM(numpy_rng=numpy_rng,
                            theano_rng=theano_rng,
                            input=layer_input,
                            n_visible=input_size,
                            n_hidden=hidden_layers_sizes[i],
                            W=sigmoid_layer.W,
                            hbias=sigmoid_layer.b)
            self.rbm_layers.append(rbm_layer)

        # We now need to add a logistic layer on top of the MLP
        self.logLayer = LogisticRegression(
            input=self.sigmoid_layers[-1].output,
            n_in=hidden_layers_sizes[-1],
            n_out=n_outs)
        self.params.extend(self.logLayer.params)
        
        # compute the cost for second phase of training, defined as the
        # negative log likelihood of the logistic regression (output) layer
        self.finetune_cost = self.logLayer.negative_log_likelihood(self.y)

        # compute the gradients with respect to the model parameters
        # symbolic variable that points to the number of errors made on the
        # minibatch given by self.x and self.y
        self.errors = self.logLayer.errors(self.y)

    def pretraining_functions(self, train_set_x, batch_size, k):
        '''Generates a list of functions, for performing one step of
        gradient descent at a given layer. The function will require
        as input the minibatch index, and to train an RBM you just
        need to iterate, calling the corresponding function on all
        minibatch indexes.

        :type train_set_x: theano.tensor.TensorType
        :param train_set_x: Shared var. that contains all datapoints used
                            for training the RBM
        :type batch_size: int
        :param batch_size: size of a [mini]batch
        :param k: number of Gibbs steps to do in CD-k / PCD-k

        '''

        # index to a [mini]batch
        index = T.lscalar('index')  # index to a minibatch
        learning_rate = T.scalar('lr')  # learning rate to use

        # number of batches
        n_batches = train_set_x.get_value(borrow=True).shape[0] / batch_size
        # begining of a batch, given `index`
        batch_begin = index * batch_size
        # ending of a batch given `index`
        batch_end = batch_begin + batch_size

        pretrain_fns = []
        for rbm in self.rbm_layers:

            # get the cost and the updates list
            # using CD-k here (persisent=None) for training each RBM.
            # TODO: change cost function to reconstruction error
            cost, updates = rbm.get_cost_updates(learning_rate,
                                                 persistent=None, k=k)

            # compile the theano function
            fn = theano.function(
                inputs=[index, theano.Param(learning_rate, default=0.1)],
                outputs=cost,
                updates=updates,
                givens={
                    self.x: train_set_x[batch_begin:batch_end]
                }
            )
            # append `fn` to the list of functions
            pretrain_fns.append(fn)

        return pretrain_fns

    def build_finetune_functions(self, datasets, batch_size, learning_rate):
        '''Generates a function `train` that implements one step of
        finetuning, a function `validate` that computes the error on a
        batch from the validation set, and a function `test` that
        computes the error on a batch from the testing set

        :type datasets: list of pairs of theano.tensor.TensorType
        :param datasets: It is a list that contain all the datasets;
                        the has to contain three pairs, `train`,
                        `valid`, `test` in this order, where each pair
                        is formed of two Theano variables, one for the
                        datapoints, the other for the labels
        :type batch_size: int
        :param batch_size: size of a minibatch
        :type learning_rate: float
        :param learning_rate: learning rate used during finetune stage

        '''

        (train_set_x, train_set_y) = datasets[0]
        (valid_set_x, valid_set_y) = datasets[1]
        (test_set_x, test_set_y) = datasets[2]

        # compute number of minibatches for training, validation and testing
        n_valid_batches = valid_set_x.get_value(borrow=True).shape[0]
        n_valid_batches /= batch_size
        n_test_batches = test_set_x.get_value(borrow=True).shape[0]
        n_test_batches /= batch_size

        index = T.lscalar('index')  # index to a [mini]batch

        # compute the gradients with respect to the model parameters
        gparams = T.grad(self.finetune_cost, self.params)

        # compute list of fine-tuning updates
        updates = []
        for param, gparam in zip(self.params, gparams):
            updates.append((param, param - gparam * learning_rate))

        train_fn = theano.function(
            inputs=[index],
            outputs=self.finetune_cost,
            updates=updates,
            givens={
                self.x: train_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
                self.y: train_set_y[
                    index * batch_size: (index + 1) * batch_size
                ]
            }
        )

        test_score_i = theano.function(
            [index],
            self.errors,
            givens={
                self.x: test_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
                self.y: test_set_y[
                    index * batch_size: (index + 1) * batch_size
                ]
            }
        )

        valid_score_i = theano.function(
            [index],
            self.errors,
            givens={
                self.x: valid_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
                self.y: valid_set_y[
                    index * batch_size: (index + 1) * batch_size
                ]
            }
        )

        # Create a function that scans the entire validation set
        def valid_score():
            return [valid_score_i(i) for i in xrange(n_valid_batches)]

        # Create a function that scans the entire test set
        def test_score():            
            return [test_score_i(i) for i in xrange(n_test_batches)]

        return train_fn, valid_score, test_score
    
    def predict_fn(self, train_set_x):
        self.result = theano.function(
        inputs=[self.x],
        outputs=[self.logLayer.p_y_given_x])
        
        return self.result(train_set_x)
    
    def get_hidden_activations(self, train_set_x):
        self.result = theano.function(
        inputs=[self.x],
        outputs=[self.sigmoid_layers[0].output])
        
        return self.result(train_set_x)

def test_DBN(finetune_lr=0.1, pretraining_epochs=3,
             pretrain_lr=0.01, k=1, training_epochs=1000,
             dataset='mnist.pkl.gz', batch_size=10, data_dir='../data/faces_small', 
             output_dir='../outputs/default',
             hidden_layers=[1000, 1000, 2000], image_dim=28):
    """
    Demonstrates how to train and test a Deep Belief Network.

    This is demonstrated on MNIST.

    :type finetune_lr: float
    :param finetune_lr: learning rate used in the finetune stage
    :type pretraining_epochs: int
    :param pretraining_epochs: number of epoch to do pretraining
    :type pretrain_lr: float
    :param pretrain_lr: learning rate to be used during pre-training
    :type k: int
    :param k: number of Gibbs steps in CD/PCD
    :type training_epochs: int
    :param training_epochs: maximal number of iterations ot run the optimizer
    :type dataset: string
    :param dataset: path the the pickled dataset
    :type batch_size: int
    :param batch_size: the size of a minibatch
    """
    
    #datasets = load_data(dataset)
    #dataset = '/home/thibaud/Research/DeepLearningTutorials/data/24x24_10_Europe'

    #list_of_datasets =

    datasets, test_set = load_faces(data_dir,image_dim)

    pre_train_set_x, pre_train_set_y = datasets[3]
    train_set_x, train_set_y = datasets[0]
    valid_set_x, valid_set_y = datasets[1]
    test_set_x, test_set_y = datasets[2]
    
    # Create outputdir:
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    if not os.path.isdir(output_dir+'/dbn_plots'):
            os.makedirs(output_dir+'/dbn_plots')
    
    # compute number of minibatches for pretraining:
    n_train_batches = pre_train_set_x.get_value(borrow=True).shape[0] / batch_size

    # numpy random generator
    numpy_rng = numpy.random.RandomState(123)
    print '... building the model'
    # construct the Deep Belief Network
    
    dbn = DBN(numpy_rng=numpy_rng, n_ins=image_dim * image_dim,
              hidden_layers_sizes=hidden_layers,
              n_outs=2)
    ### LOOK ABOVE: n_outs=2 for smile vs. no smile!  or 10 for digits!
    #pred_fn = dbn.predict_fn()
    
    # start-snippet-2
    #########################
    # PRETRAINING THE MODEL #
    #########################
    print '... getting the pretraining functions'
    pretraining_fns = dbn.pretraining_functions(train_set_x=pre_train_set_x,
                                                batch_size=batch_size,
                                                k=k)

    print '... pre-training the model'
    start_time = time.clock()
    ## Pre-train layer-wise
    for i in xrange(dbn.n_layers):
        # go through pretraining epochs
        for epoch in xrange(pretraining_epochs):
            # go through the training set
            c = []
            for batch_index in xrange(n_train_batches):
                c.append(pretraining_fns[i](index=batch_index,
                                            lr=pretrain_lr))
            print 'Pre-training layer %i, epoch %d, cost ' % (i, epoch),
            print numpy.mean(c)
            
            ## DAN ADDED: Construct image from the weight matrix
            #print (len(dbn.sigmoid_layers[0].W.get_value(borrow=True)[1,:]))
            if (epoch) % 10 == 0 and i==0:
                image = Image.fromarray(
                    tile_raster_images(
                        X=dbn.sigmoid_layers[0].W.get_value(borrow=True).T,
                        #X=dbm.W.get_value(borrow=True).T,
                        img_shape=(image_dim, image_dim),
                        tile_shape=(10, 10),
                        tile_spacing=(1, 1)
                    )
                )
                image.save(output_dir+'/dbn_plots/DBN_filters_at_epoch_%i.png' % epoch)
                
                temp = dbn.get_hidden_activations(train_set_x.get_value(borrow=True))
                #temp =  numpy.dot(train_set_x.get_value(borrow=True).T,temp[0])
                temp=temp[0]*255
                temp=temp.T
                im = Image.fromarray(temp.astype(numpy.uint8))
                im.save(output_dir+'/dbn_plots/Debug_DBN_filters_at_epoch_%i.png' % epoch)

    end_time = time.clock()
    # end-snippet-2
    print >> sys.stderr, ('The pretraining code for file ' +
                          os.path.split(__file__)[1] +
                          ' ran for %.2fm' % ((end_time - start_time) / 60.))
    ########################
    # FINETUNING THE MODEL #
    ########################

    # compute number of minibatches for training, validation and testing:
    n_train_batches = train_set_x.get_value(borrow=True).shape[0] / batch_size

    # get the training, validation and testing function for the model
    print '... getting the finetuning functions'
    train_fn, validate_model, test_model = dbn.build_finetune_functions(
        datasets=datasets,
        batch_size=batch_size,
        learning_rate=finetune_lr
    )

    print '... finetuning the model'
    # early-stopping parameters
    patience = 4 * n_train_batches  # look as this many examples regardless
    patience_increase = 2.    # wait this much longer when a new best is
                              # found
    improvement_threshold = 0.995  # a relative improvement of this much is
                                   # considered significant
    validation_frequency = min(n_train_batches, patience / 2)
                                  # go through this many
                                  # minibatches before checking the network
                                  # on the validation set; in this case we
                                  # check every epoch

    best_validation_loss = numpy.inf
    test_score = 0.
    start_time = time.clock()

    done_looping = False
    epoch = 0

    while (epoch < training_epochs) and (not done_looping):
        epoch = epoch + 1
        for minibatch_index in xrange(n_train_batches):

            minibatch_avg_cost = train_fn(minibatch_index)
            iter = (epoch - 1) * n_train_batches + minibatch_index

            if (iter + 1) % validation_frequency == 0:

                validation_losses = validate_model()
                this_validation_loss = numpy.mean(validation_losses)
                print(
                    'epoch %i, minibatch %i/%i, validation error %f %%'
                    % (
                        epoch,
                        minibatch_index + 1,
                        n_train_batches,
                        this_validation_loss * 100.
                    )
                )

                # if we got the best validation score until now
                if this_validation_loss < best_validation_loss:

                    #improve patience if loss improvement is good enough
                    if (
                        this_validation_loss < best_validation_loss *
                        improvement_threshold
                    ):
                        patience = max(patience, iter * patience_increase)

                    # save best validation score and iteration number
                    best_validation_loss = this_validation_loss
                    best_iter = iter

                    # test it on the test set
                    test_losses = test_model()
                    test_score = numpy.mean(test_losses)
                    print(('     epoch %i, minibatch %i/%i, test error of '
                           'best model %f %%') %
                          (epoch, minibatch_index + 1, n_train_batches,
                           test_score * 100.))

            if patience <= iter:
                done_looping = True
                break

    end_time = time.clock()
    print(
        (
            'Optimization complete with best validation score of %f %%, '
            'obtained at iteration %i, '
            'with test performance %f %%'
        ) % (best_validation_loss * 100., best_iter + 1, test_score * 100.)
    )
    print >> sys.stderr, ('The fine tuning code for file ' +
                          os.path.split(__file__)[1] +
                          ' ran for %.2fm' % ((end_time - start_time)
                                              / 60.))
    
    #print theano.pp(dbn.sigmoid_layers[-1].output)
    #print theano.pp(T.nnet.softmax(T.dot(dbn.sigmoid_layers[-1].output, dbn.logLayer.W) + dbn.logLayer.b))

    test_input = identify_face = numpy.zeros((1,2304), dtype=numpy.float32)
    dat = test_set[0]
    predicted_label = dbn.predict_fn(dat.astype(numpy.float32, copy=False))
    predicted_label = numpy.asarray(predicted_label)
    a = numpy.asarray([ test_set[1] ])

    a1 = numpy.concatenate((a.T, predicted_label[0]), axis=1)
    filename = output_dir+'/ROC.csv'
    numpy.savetxt(filename, a1, delimiter=",")
    
    ## SAVE THE MODEL:
    # To save the DBN model we need to store (for each layer) the:
    # Number of hidden units (should be able to deduce this from W but stored it anyway).
    # The weights (sigmoid_layer.W)
    # The hidden bias units (sigmoid_layer.b)
    filename = output_dir+'/model_parameters'
    #save_file = open(filename)
    pickler = cPickle.Pickler('output', -1)
    pickler.dump(hls)
    for inter in range(0,3):
        pickler.dump(dbn.sigmoid_layers[inter].W.get_value(borrow=True))
        pickler.dump(dbn.sigmoid_layers[inter].hbias.get_value(borrow=True))

## Parse the command-line arguments:
def main(argv):
   inputfile = ''
   outputfile = ''
   try:
      opts, args = getopt.getopt(argv,"hi:o:",["ifile=","ofile=","hidden_layers="])
   except getopt.GetoptError:
      print 'test.py -i <inputfile> -o <outputfile> -h <hidden_layers>'
      sys.exit(2)
   for opt, arg in opts:
      if opt == '-h':
         print 'test.py -i <inputfile> -o <outputfile> -h <hidden_layers>'
         sys.exit()
      elif opt in ("-i", "--ifile"):
         inputfile = arg
      elif opt in ("-o", "--ofile"):
         outputfile = arg
      elif opt in ("-h", "--hidden_layers"):
         hidden_layers = arg
   print 'Input file is "', inputfile
   print 'Output file is "', outputfile
   print 'Hidden Layers are "', hidden_layers
   return inputfile, outputfile, hdden_layers

if __name__ == '__main__':
    
    parser = optparse.OptionParser()
    parser.add_option('-i', dest="inputfile", action="store")
    parser.add_option('-o', dest="outputfile", action="store")
    parser.add_option('-r', dest="image_dim", action="store", type="int")
    parser.add_option('-l', dest="layers", nargs=2, action="store", type="int")
    parser.add_option('-p', dest="pt_epochs", nargs=1, action="store", type="int")
    parser.add_option('-f', dest="ft_epochs", nargs=1, action="store", type="int")
    options, remainder = parser.parse_args()
    print 'INPUT:           '+options.inputfile
    print 'OUTPUT:          '+options.outputfile
    print 'IMAGE DIM.:      '+str(options.image_dim)
    print 'HID. LAYERS:     '+str(options.layers)
    print 'PT. EPOCHS:      '+str(options.pt_epochs)
    print 'FT. EPOCHS:      '+str(options.ft_epochs)
    
    hls = numpy.asarray(options.layers)
    test_DBN(data_dir=options.inputfile, output_dir=options.outputfile, hidden_layers=hls, pretraining_epochs=options.pt_epochs, training_epochs=options.ft_epochs, image_dim=options.image_dim)

