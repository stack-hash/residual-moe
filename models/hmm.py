"""
This example shows how to marginalize out discrete model variables in Pyro.

This combines Stochastic Variational Inference (SVI) with a
variable elimination algorithm, where we use enumeration to exactly
marginalize out some variables from the ELBO computation. We might
call the resulting algorithm collapsed SVI or collapsed SGVB (i.e
collapsed Stochastic Gradient Variational Bayes). In the case where
we exactly sum out all the latent variables (as is the case here),
this algorithm reduces to a form of gradient-based Maximum
Likelihood Estimation.

To marginalize out discrete variables ``x`` in Pyro's SVI:

1. Verify that the variable dependency structure in your model
    admits tractable inference, i.e. the dependency graph among
    enumerated variables should have narrow treewidth.
2. Annotate each target each such sample site in the model
    with ``infer={"enumerate": "parallel"}``
3. Ensure your model can handle broadcasting of the sample values
    of those variables
4. Use the ``TraceEnum_ELBO`` loss inside Pyro's ``SVI``.

Note that empirical results for the models defined here can be found in
reference [1]. This paper also includes a description of the "tensor
variable elimination" algorithm that Pyro uses under the hood to
marginalize out discrete latent variables.

References

1. "Tensor Variable Elimination for Plated Factor Graphs",
Fritz Obermeyer, Eli Bingham, Martin Jankowiak, Justin Chiu,
Neeraj Pradhan, Alexander Rush, Noah Goodman. https://arxiv.org/abs/1902.03210
"""





import argparse
import logging

import torch
import torch.nn as nn
from torch.distributions import constraints

import models.dmm.polyphonic_data_loader as poly
# import dmm.polyphonic_data_loader as poly
import pyro
import pyro.distributions as dist
from pyro import poutine
from pyro.contrib.autoguide import AutoDelta
from pyro.infer import SVI, JitTraceEnum_ELBO, TraceEnum_ELBO
from pyro.optim import Adam
from pyro.util import ignore_jit_warnings

logging.basicConfig(format='%(relativeCreated) 9d %(message)s', level=logging.INFO)


# Let's start with a simple Hidden Markov Model.
#
#     x[t-1] --> x[t] --> x[t+1]
#        |        |         |
#        V        V         V
#     y[t-1]     y[t]     y[t+1]
#
# This model includes a plate for the data_dim = 88 keys on the piano. This
# model has two "style" parameters probs_x and probs_y that we'll draw from a
# prior. The latent state is x, and the observed state is y. We'll drive
# probs_* with the guide, enumerate over x, and condition on y.
#
# Importantly, the dependency structure of the enumerated variables has
# narrow treewidth, therefore admitting efficient inference by message passing.
# Pyro's TraceEnum_ELBO will find an efficient message passing scheme if one
# exists.
def model_0(sequences, lengths, args, batch_size=None, include_prior=True):
    assert not torch._C._get_tracing_state()
    num_sequences, max_length, data_dim = sequences.shape
    with poutine.mask(mask=include_prior):
        # Our prior on transition probabilities will be:
        # stay in the same state with 90% probability; uniformly jump to another
        # state with 10% probability.
        probs_x = pyro.sample("probs_x",
                              dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1)
                                  .to_event(1))
        # We put a weak prior on the conditional probability of a tone sounding.
        # We know that on average about 4 of 88 tones are active, so we'll set a
        # rough weak prior of 10% of the notes being active at any one time.
        probs_y = pyro.sample("probs_y",
                              dist.Beta(0.1, 0.9)
                                  .expand([args.hidden_dim, data_dim])
                                  .to_event(2))
    # In this first model we'll sequentially iterate over sequences in a
    # minibatch; this will make it easy to reason about tensor shapes.
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    for i in pyro.plate("sequences", len(sequences), batch_size):
        length = lengths[i]
        sequence = sequences[i, :length]
        x = 0
        for t in pyro.markov(list(range(length))):
            # On the next line, we'll overwrite the value of x with an updated
            # value. If we wanted to record all x values, we could instead
            # write x[t] = pyro.sample(...x[t-1]...).
            x = pyro.sample("x_{}_{}".format(i, t), dist.Categorical(probs_x[x]),
                            infer={"enumerate": "parallel"})
            with tones_plate:
                pyro.sample("y_{}_{}".format(i, t), dist.Bernoulli(probs_y[x.squeeze(-1)]),
                            obs=sequence[t])
# To see how enumeration changes the shapes of these sample sites, we can use
# the Trace.format_shapes() to print shapes at each site:
# $ python examples/hmm.py -m 0 -n 1 -b 1 -t 5 --print-shapes
# ...
#  Sample Sites:
#   probs_x dist          | 16 16
#          value          | 16 16
#   probs_y dist          | 16 88
#          value          | 16 88
#     tones dist          |
#          value       88 |
# sequences dist          |
#          value        1 |
#   x_178_0 dist          |
#          value    16  1 |
#   y_178_0 dist    16 88 |
#          value       88 |
#   x_178_1 dist    16  1 |
#          value 16  1  1 |
#   y_178_1 dist 16  1 88 |
#          value       88 |
#   x_178_2 dist 16  1  1 |
#          value    16  1 |
#   y_178_2 dist    16 88 |
#          value       88 |
#   x_178_3 dist    16  1 |
#          value 16  1  1 |
#   y_178_3 dist 16  1 88 |
#          value       88 |
#   x_178_4 dist 16  1  1 |
#          value    16  1 |
#   y_178_4 dist    16 88 |
#          value       88 |
#
# Notice that enumeration (over 16 states) alternates between two dimensions:
# -2 and -3.  If we had not used pyro.markov above, each enumerated variable
# would need its own enumeration dimension.


# Next let's make our simple model faster in two ways: first we'll support
# vectorized minibatches of data, and second we'll support the PyTorch jit
# compiler.  To add batch support, we'll introduce a second plate "sequences"
# and randomly subsample data to size batch_size.  To add jit support we
# silence some warnings and try to avoid dynamic program structure.

# Note that this is the "HMM" model in reference [1] (with the difference that
# in [1] the probabilities probs_x and probs_y are not MAP-regularized with
# Dirichlet and Beta distributions for any of the models)


# def pred_model_1(sequences, lengths, args, include_prior=True):
#     with ignore_jit_warnings():
#         num_sequences, max_length, data_dim = map(int, sequences.shape)
#         assert lengths.shape == (num_sequences,)
#         assert lengths.max() <= max_length
#
#     ip = torch.ByteTensor([include_prior]).to(sequences.device)
#     with poutine.mask(mask=ip):
#         probs_x = pyro.sample("probs_x",
#                               dist.Dirichlet(0.9 * torch.eye(args.hidden_dim, device=sequences.device) + 0.1)
#                                   .to_event(1))
#         probs_y = pyro.sample("probs_y",
#                               dist.Beta(0.1 * torch.ones(1, device=sequences.device), 0.9)
#                                   .expand([args.hidden_dim, data_dim])
#                                   .to_event(2))
#     tones_plate = pyro.plate("tones", data_dim, dim=-1, device=sequences.device)
#     tones_plate.use_cuda = str(sequences.device).startswith("cuda")
#
#     # NOTE: no subsample here!!
#     with pyro.plate("sequences", num_sequences, dim=-2, device=sequences.device):
#         x = 0
#         for t in pyro.markov(range(lengths.max())):
#             with poutine.mask(mask=(t < lengths).unsqueeze(-1)):
#                 x = pyro.sample("x_{}".format(t), dist.Categorical(probs_x[x]),
#                                 infer={"enumerate": "parallel"})
#                 with tones_plate:
#                     y = pyro.sample("y_{}".format(t), dist.Bernoulli(probs_y[x.squeeze(-1)]))


def model_lds(sequences, lengths, args, include_prior=True, pred_mode=False):
    # Sometimes it is safe to ignore jit warnings. Here we use the
    # pyro.util.ignore_jit_warnings context manager to silence warnings about
    # conversion to integer, since we know all three numbers will be the same
    # across all invocations to the model.
    device = sequences.device
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = list(map(int, sequences.shape))
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    ip = torch.ByteTensor([include_prior]).to(sequences.device)
    with poutine.mask(mask=ip):

        # put global vars here (e.g., A, B, r)

        A = torch.eyes((args.hidden_dim, args.hidden_dim),
                       device=device) * 0.9 + 0.1

        Q = torch.eyes((args.hidden_dim,args.hidden_dim),
                       device=device) * 1e-5

        # probs_x = pyro.sample("probs_x",
        #                       dist.Dirichlet(0.9 * torch.eye(args.hidden_dim,
        #                                      device=device) + 0.1)
        #                           .to_event(1))
        probs_y = pyro.sample("probs_y",
                              dist.Beta(0.1 * torch.ones(1, device=device), 0.9)
                                  .expand([args.hidden_dim, data_dim])
                                  .to_event(2))

    tones_plate = pyro.plate("tones", data_dim, dim=-1, device=sequences.device)
    tones_plate.use_cuda = str(sequences.device).startswith("cuda")
    # We subsample batch_size items out of num_sequences items. Note that since
    # we're using dim=-1 for the notes plate, we need to batch over a different
    # dimension, here dim=-2.
    # pred_container = {}

    # NOTE: no subsample here!!

    with pyro.plate("sequences", num_sequences, dim=-2, device=device):

        # put local vars here (e.g., z)

        # lengths = lengths[batch]
        x = 0
        # If we are not using the jit, then we can vary the program structure
        # each call by running for a dynamically determined number of time
        # steps, lengths.max(). However if we are using the jit, then we try to
        # keep a single program structure for all minibatches; the fixed
        # structure ends up being faster since each program structure would
        # need to trigger a new jit compile stage.
        for t in pyro.markov(list(range(lengths.max()))):
            with poutine.mask(mask=(t < lengths).unsqueeze(-1).to(device)):

                x_noise = pyro.sample("x_noise",
                                      dist.Normal(0, Q),
                                      infer={"enumerate": "parallel"})

                x = A * x + x_noise
                # x = pyro.sample("x_{}".format(t), dist.Normal(A*x, x_noise),
                #                 infer={"enumerate": "parallel"})

                # x = pyro.sample("x_{}".format(t), dist.Categorical(probs_x[x]),
                #                 infer={"enumerate": "parallel"})
                with tones_plate:
                    pyro.sample("y_{}".format(t), dist.Bernoulli(probs_y[x.squeeze(-1)]),
                                obs=None if pred_mode else sequences[:, t])



# Let's see how batching changes the shapes of sample sites:
# $ python examples/hmm.py -m 1 -n 1 -t 5 --batch-size=10 --print-shapes
# ...
#  Sample Sites:
#   probs_x dist             | 16 16
#          value             | 16 16
#   probs_y dist             | 16 88
#          value             | 16 88
#     tones dist             |
#          value          88 |
# sequences dist             |
#          value          10 |
#       x_0 dist       10  1 |
#          value    16  1  1 |
#       y_0 dist    16 10 88 |
#          value       10 88 |
#       x_1 dist    16 10  1 |
#          value 16  1  1  1 |
#       y_1 dist 16  1 10 88 |
#          value       10 88 |
#       x_2 dist 16  1 10  1 |
#          value    16  1  1 |
#       y_2 dist    16 10 88 |
#          value       10 88 |
#       x_3 dist    16 10  1 |
#          value 16  1  1  1 |
#       y_3 dist 16  1 10 88 |
#          value       10 88 |
#       x_4 dist 16  1 10  1 |
#          value    16  1  1 |
#       y_4 dist    16 10 88 |
#          value       10 88 |
#
# Notice that we're now using dim=-2 as a batch dimension (of size 10),
# and that the enumeration dimensions are now dims -3 and -4.



# Next let's consider a neural HMM model.
#
#     x[t-1] --> x[t] --> x[t+1]   } standard HMM +
#        |        |         |
#        V        V         V
#     y[t-1] --> y[t] --> y[t+1]   } neural likelihood
#
# First let's define a neural net to generate y logits.
class TonesGenerator(nn.Module):
    def __init__(self, args, data_dim):
        self.args = args
        self.data_dim = data_dim
        super(TonesGenerator, self).__init__()
        self.x_to_hidden = nn.Linear(args.hidden_dim, args.nn_dim)
        self.y_to_hidden = nn.Linear(args.nn_channels * data_dim, args.nn_dim)
        self.conv = nn.Conv1d(1, args.nn_channels, 3, padding=1)
        self.hidden_to_logits = nn.Linear(args.nn_dim, data_dim)
        self.relu = nn.ReLU()

    def forward(self, x, y):
        # Check dimension of y so this can be used with and without enumeration.
        if y.dim() < 2:
            y = y.unsqueeze(0)

        # Hidden units depend on two inputs: a one-hot encoded categorical variable x, and
        # a bernoulli variable y. Whereas x will typically be enumerated, y will be observed.
        # We apply x_to_hidden independently from y_to_hidden, then broadcast the non-enumerated
        # y part up to the enumerated x part in the + operation.
        x_onehot = y.new_zeros(x.shape[:-1] + (self.args.hidden_dim,)).scatter_(-1, x, 1)
        y_conv = self.relu(self.conv(y.unsqueeze(-2))).reshape(y.shape[:-1] + (-1,))
        h = self.relu(self.x_to_hidden(x_onehot) + self.y_to_hidden(y_conv))
        return self.hidden_to_logits(h)


# We will create a single global instance later.
tones_generator = None


# The neural HMM model now uses tones_generator at each time step.
#
# Note that this is the "nnHMM" model in reference [1].

def pred_model_5(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = list(map(int, sequences.shape))
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length

    # Initialize a global module instance if needed.
    global tones_generator
    if tones_generator is None:
        tones_generator = TonesGenerator(args, data_dim)
    pyro.module("tones_generator", tones_generator)

    with poutine.mask(mask=include_prior):
        probs_x = pyro.sample("probs_x",
                              dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1)
                                  .to_event(1))
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        x = 0
        y = torch.zeros(data_dim)
        for t in pyro.markov(list(range(max_length if args.jit else lengths.max()))):
            with poutine.mask(mask=(t < lengths).unsqueeze(-1)):
                x = pyro.sample("x_{}".format(t), dist.Categorical(probs_x[x]),
                                infer={"enumerate": "parallel"})
                # Note that since each tone depends on all tones at a previous time step
                # the tones at different time steps now need to live in separate plates.
                with pyro.plate("tones_{}".format(t), data_dim, dim=-1):
                    y = pyro.sample("y_{}".format(t),
                                    dist.Bernoulli(logits=tones_generator(x, y)))


def model_5(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = list(map(int, sequences.shape))
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length

    # Initialize a global module instance if needed.
    global tones_generator
    if tones_generator is None:
        tones_generator = TonesGenerator(args, data_dim)
    pyro.module("tones_generator", tones_generator)

    with poutine.mask(mask=include_prior):
        probs_x = pyro.sample("probs_x",
                              dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1)
                                  .to_event(1))
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        x = 0
        y = torch.zeros(data_dim)
        for t in pyro.markov(list(range(max_length if args.jit else lengths.max()))):
            with poutine.mask(mask=(t < lengths).unsqueeze(-1)):
                x = pyro.sample("x_{}".format(t), dist.Categorical(probs_x[x]),
                                infer={"enumerate": "parallel"})
                # Note that since each tone depends on all tones at a previous time step
                # the tones at different time steps now need to live in separate plates.
                with pyro.plate("tones_{}".format(t), data_dim, dim=-1):
                    y = pyro.sample("y_{}".format(t),
                                    dist.Bernoulli(logits=tones_generator(x, y)),
                                    obs=sequences[batch, t])


models = {name[len('model_'):]: model
          for name, model in list(globals().items())
          if name.startswith('model_')}



def model_hmm(sequences, lengths, args, include_prior=True, pred_mode=False,
              yprob_store={}):
    # Sometimes it is safe to ignore jit warnings. Here we use the
    # pyro.util.ignore_jit_warnings context manager to silence warnings about
    # conversion to integer, since we know all three numbers will be the same
    # across all invocations to the model.
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = list(map(int, sequences.shape))
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    ip = torch.ByteTensor([include_prior]).to(sequences.device)
    with poutine.mask(mask=ip):

        probs_x = pyro.sample("probs_x",
                              dist.Dirichlet(0.9 * torch.eye(args.hidden_dim, device=sequences.device) + 0.1)
                                  .to_event(1))
        probs_y = pyro.sample("probs_y",
                              dist.Beta(0.1 * torch.ones(1, device=sequences.device), 0.9)
                                  .expand([args.hidden_dim, data_dim])
                                  .to_event(2))
    tones_plate = pyro.plate("tones", data_dim, dim=-1, device=sequences.device)
    tones_plate.use_cuda = str(sequences.device).startswith("cuda")
    # We subsample batch_size items out of num_sequences items. Note that since
    # we're using dim=-1 for the notes plate, we need to batch over a different
    # dimension, here dim=-2.
    # pred_container = {}

    # NOTE: no subsample here!!

    with pyro.plate("sequences", num_sequences, dim=-2, device=sequences.device):
        # lengths = lengths[batch]
        x = 0
        # If we are not using the jit, then we can vary the program structure
        # each call by running for a dynamically determined number of time
        # steps, lengths.max(). However if we are using the jit, then we try to
        # keep a single program structure for all minibatches; the fixed
        # structure ends up being faster since each program structure would
        # need to trigger a new jit compile stage.
        for t in pyro.markov(list(range(lengths.max()))):
            with poutine.mask(mask=(t < lengths).unsqueeze(-1).to(sequences.device)):
                x = pyro.sample("x_{}".format(t), dist.Categorical(probs_x[x]),
                                infer={"enumerate": "parallel"})
                with tones_plate:
                    y = pyro.sample("y_{}".format(t), dist.Bernoulli(probs_y[x.squeeze(-1)]),
                                obs=None if pred_mode else sequences[:, t])
                    yprob_store["yprob_{}".format(t)] = probs_y[x.squeeze(-1)]


def model_hmm_1(sequences, lengths, args, batch_size=None, include_prior=True,
                pred_mode=False, yprob_store={}):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = list(map(int, sequences.shape))
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    include_prior = torch.BoolTensor([include_prior]).to(sequences.device)
    with poutine.mask(mask=include_prior):
        probs_x = pyro.sample("probs_x",
                              dist.Dirichlet(
                                  0.1 * torch.eye(args.hidden_dim,
                                                  device=sequences.device)
                                  + 0.01)
                              .to_event(1))

        if (probs_x != probs_x).sum() > 0:
            raise RuntimeError("NaN")
        assert (probs_x != probs_x).sum() == 0, "NaN!"

        probs_y = pyro.sample("probs_y",
                              dist.Beta(0.2 * torch.ones(1, device=sequences.device)
                                        , 0.99)
                              .expand([args.hidden_dim, data_dim])
                              .to_event(2))

        assert (probs_y != probs_y).sum() == 0, "NaN!"

    tones_plate = pyro.plate("tones", data_dim, dim=-1, device=sequences.device)
    tones_plate.use_cuda = str(sequences.device).startswith("cuda")
    # We subsample batch_size items out of num_sequences items. Note that since
    # we're using dim=-1 for the notes plate, we need to batch over a different
    # dimension, here dim=-2.
    with pyro.plate("sequences", num_sequences, batch_size,
                    dim=-2, device=sequences.device) as batch:
        lengths = lengths[batch]
        x = 0
        # If we are not using the jit, then we can vary the program structure
        # each call by running for a dynamically determined number of time
        # steps, lengths.max(). However if we are using the jit, then we try to
        # keep a single program structure for all minibatches; the fixed
        # structure ends up being faster since each program structure would
        # need to trigger a new jit compile stage.
        for t in pyro.markov(
                list(range(max_length if args.jit else lengths.max()))):
            with poutine.mask(mask=(t < lengths).unsqueeze(-1).to(sequences.device)):
                x = pyro.sample("x_{}".format(t),
                                dist.Categorical(probs_x[x]),
                                infer={"enumerate": "parallel"})

                assert (x != x).sum() == 0, "NaN!"

                with tones_plate:
                    pyro.sample("y_{}".format(t),
                                dist.Bernoulli(probs_y[x.squeeze(-1)]),
                                obs=None if pred_mode else sequences[batch, t])
                    pred = probs_y[x.squeeze(-1)]
                    pred = nn.functional.sigmoid(pred)  # softplus
                    assert (pred != pred).sum() == 0, "NaN!"

                    yprob_store["yprob_{}".format(t)] = pred


def get_preds(autoguide, model, sequences, lengths, args):
    guide_trace = poutine.trace(autoguide).get_trace(sequences, lengths, args)
    # assuming that the original model took in data as (x, y) where y is observed

    # preds = poutine.replay(model, guide_trace)(sequences, lengths, args)
    # preds = [preds[key] for key in sorted(preds.keys())]
    # preds = torch.stack([torch.stack(p) for p in preds]).squeeze(1)
    probs = {}

    model_trace = poutine.trace(poutine.replay(model, trace=guide_trace)).get_trace(
        sequences, lengths, args, pred_mode=True, yprob_store=probs)
    preds = {}
    for name, node in list(model_trace.nodes.items()):
        if node["name"].startswith("y_"):
            preds[node["name"]] = node["value"]

    def extract_seq_val(x):
        x = {int(k.split('_')[-1]): v for k, v in x.items()}
        x = [x[key] for key in sorted(x.keys())]
        x = torch.stack(x)
        x = x.permute(1, 0, 2)
        x = x.to(sequences.device)
        return x

    probs = extract_seq_val(probs)
    preds = extract_seq_val(preds)
    return preds, probs  # samples from the predictive distribution.


def main(args):
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')

    print('Loading data')
    data = poly.load_data(poly.JSB_CHORALES)

    print('-' * 40)
    model = models[args.model]
    print('Training {} on {} sequences'.format(
        model.__name__, len(data['train']['sequences'])))
    sequences = data['train']['sequences']
    lengths = data['train']['sequence_lengths']

    # find all the notes that are present at least once in the training set
    present_notes = ((sequences == 1).sum(0).sum(0) > 0)
    # remove notes that are never played (we remove 37/88 notes)
    sequences = sequences[..., present_notes]

    if args.truncate:
        lengths.clamp_(max=args.truncate)
        sequences = sequences[:, :args.truncate]
    num_observations = float(lengths.sum())
    pyro.set_rng_seed(0)
    pyro.clear_param_store()
    pyro.enable_validation(True)

    # We'll train using MAP Baum-Welch, i.e. MAP estimation while marginalizing
    # out the hidden state x. This is accomplished via an automatic guide that
    # learns point estimates of all of our conditional probability tables,
    # named probs_*.
    guide = AutoDelta(poutine.block(model, expose_fn=lambda msg: msg["name"].startswith("probs_")))

    # To help debug our tensor shapes, let's print the shape of each site's
    # distribution, value, and log_prob tensor. Note this information is
    # automatically printed on most errors inside SVI.
    if args.print_shapes:
        first_available_dim = -2 if model is model_0 else -3
        guide_trace = poutine.trace(guide).get_trace(
            sequences, lengths, args=args, batch_size=args.batch_size)
        model_trace = poutine.trace(
            poutine.replay(poutine.enum(model, first_available_dim), guide_trace)).get_trace(
            sequences, lengths, args=args, batch_size=args.batch_size)
        print(model_trace.format_shapes())

    # Enumeration requires a TraceEnum elbo and declaring the max_plate_nesting.
    # All of our models have two plates: "data" and "tones".
    Elbo = JitTraceEnum_ELBO if args.jit else TraceEnum_ELBO
    elbo = Elbo(max_plate_nesting=1 if model is model_0 else 2)
    optim = Adam({'lr': args.learning_rate})
    svi = SVI(model, guide, optim, elbo)

    # We'll train on small minibatches.
    print('TRAIN RUN ==== ')
    print('Step\tLoss')
    for step in range(args.num_steps):
        loss = svi.step(sequences, lengths, args=args, batch_size=args.batch_size)
        print('{: >5d}\t{:.4f}'.format(step, loss / num_observations))

        test_loss = elbo.loss(model, guide, sequences, lengths, args=args,
                              include_prior=False)
        print('test loss\t{:.4f}'.format(test_loss / num_observations))

        print('PRED RUN ==== ')
        # for seq in sequences:
        preds, probs = get_preds(guide, pred_model_5, sequences, lengths, args)

        acc = (preds == sequences).sum().float().div(sequences.numel())
        print('test acc\t{:.4f}'.format(acc))

    # We evaluate on the entire training dataset,
    # excluding the prior term so our results are comparable across models.
    train_loss = elbo.loss(model, guide, sequences, lengths, args, include_prior=False)

    print('training loss = {}'.format(train_loss / num_observations))

    # Finally we evaluate on the test dataset.
    print('-' * 40)
    print('Evaluating on {} test sequences'.format(len(data['test']['sequences'])))
    sequences = data['test']['sequences'][..., present_notes]
    lengths = data['test']['sequence_lengths']
    if args.truncate:
        lengths.clamp_(max=args.truncate)
    num_observations = float(lengths.sum())

    # note that since we removed unseen notes above (to make the problem a bit easier and for
    # numerical stability) this test loss may not be directly comparable to numbers
    # reported on this dataset elsewhere.
    print('TEST RUN ==== ')
    test_loss = elbo.loss(model, guide, sequences, lengths, args=args, include_prior=False)
    print('test loss = {}'.format(test_loss / num_observations))

    # We expect models with higher capacity to perform better,
    # but eventually overfit to the training set.
    capacity = sum(value.reshape(-1).size(0)
                   for value in list(pyro.get_param_store().values()))
    print('{} capacity = {} parameters'.format(model.__name__, capacity))


if __name__ == '__main__':
    # assert pyro.__version__.startswith('0.3.1')
    parser = argparse.ArgumentParser(description="MAP Baum-Welch learning Bach Chorales")
    parser.add_argument("-m", "--model", default="1", type=str,
                        help="one of: {}".format(", ".join(sorted(models.keys()))))
    parser.add_argument("-n", "--num-steps", default=50, type=int)
    parser.add_argument("-b", "--batch-size", default=8, type=int)
    parser.add_argument("-d", "--hidden-dim", default=16, type=int)
    parser.add_argument("-nn", "--nn-dim", default=48, type=int)
    parser.add_argument("-nc", "--nn-channels", default=2, type=int)
    parser.add_argument("-lr", "--learning-rate", default=0.05, type=float)
    parser.add_argument("-t", "--truncate", type=int)
    parser.add_argument("-p", "--print-shapes", action="store_true")
    parser.add_argument('--cuda', action='store_true')
    parser.add_argument('--jit', action='store_true')
    parser.add_argument('-rp', '--raftery-parameterization', action='store_true')
    args = parser.parse_args()
    main(args)
