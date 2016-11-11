import logging
import numpy as np
from time import sleep
import dask
from distributed import Client
import scipy.stats as ss

from elfi.bo.gpy_model import GPyModel
from elfi.bo.acquisition import LcbAcquisition, SecondDerivativeNoiseMixin, RbfAtPendingPointsMixin
from elfi.utils import stochastic_optimization
from elfi.posteriors import BolfiPosterior
from .async import wait
from elfi import Discrepancy, Operation
from .utils import weighted_var
from .distributions import Prior

logger = logging.getLogger(__name__)

"""
Implementations of some ABC algorithms
"""

class ABCMethod(object):
    """Base class for ABC methods.

    Attributes
    ----------
    distance_node : Discrepancy
        The discrepancy node in inference model.
    parameter_nodes : a list of Operation
        The nodes representing the targets of inference.
    batch_size : int, optional
        The number of samples in each parallel batch (may affect performance).
    """
    def __init__(self, distance_node=None, parameter_nodes=None, batch_size=10):

        try:
            if not isinstance(distance_node, Discrepancy):
                raise TypeError
            if not all(map(lambda n: isinstance(n, Operation), parameter_nodes)):
                raise TypeError
        except TypeError:
            raise TypeError("Need to give the distance node and a list of "
                            "parameter nodes that inherit Operation.")

        self.distance_node = distance_node
        self.parameter_nodes = parameter_nodes
        self.n_params = len(parameter_nodes)
        self.batch_size = batch_size

    def sample(self, *args, **kwargs):
        """Run the sampler.

        Returns
        -------
        A dictionary with at least the following items:
        samples : list of np.arrays
            Samples from the posterior distribution of each parameter.
        """
        raise NotImplementedError

    # Run the all-accepting sampler.
    def _get_distances(self, n_samples):

        distances = self.distance_node.acquire(n_samples, batch_size=self.batch_size).compute()
        parameters = [p.acquire(n_samples, batch_size=self.batch_size).compute()
                      for p in self.parameter_nodes]

        return distances, parameters


class Rejection(ABCMethod):
    """
    Rejection sampler.
    """
    def sample(self, n, quantile=0.01, threshold=None):
        """Run the rejection sampler.

        In quantile mode, the simulator is run (n/quantile) times, returning n samples
        from the posterior.

        In threshold mode, the simulator is run n times and the number of returned
        samples will be in range [0, n].

        However, subsequent calls will reuse existing samples without
        rerunning the simulator until necessary.

        Parameters
        ----------
        n : int
            Number of samples (with quantile) or number of simulator runs (with threshold).
        quantile : float in range ]0, 1], optional
            The quantile for determining the acceptance threshold.
        threshold : float, optional
            The acceptance threshold.

        Returns
        -------
        A dictionary with items:
        samples : list of np.arrays
            Samples from the posterior distribution of each parameter.
        threshold : float
            The threshold value used in inference.
        """

        if quantile <= 0 or quantile > 1:
            raise ValueError("Quantile must be in range ]0, 1].")

        n_sim = int(n / quantile) if threshold is None else n

        distances, parameters = self._get_distances(n_sim)
        distances = distances.ravel()  # avoid unnecessary indexing

        if threshold is None:  # filter with quantile
            sorted_inds = np.argsort(distances)
            threshold = distances[ sorted_inds[n-1] ]
            accepted = sorted_inds[:n]

        else:  # filter with predefined threshold
            accepted = distances < threshold

        posteriors = [p[accepted] for p in parameters]

        return {'samples': posteriors, 'threshold': threshold}


class SMC(Rejection):
    """Likelihood-free sequential Monte Carlo sampler.

    Based on Algorithm 4 in:
    Jean-Michel Marin, Pierre Pudlo, Christian P Robert, and Robin J Ryder:
    Approximate bayesian computational methods, Statistics and Computing,
    22(6):1167–1180, 2012.

    Attributes
    ----------
    (as in Rejection)

    See Also
    --------
    `Rejection` : Basic rejection sampling.
    """

    def infer(self, n_populations, schedule):
        """Run SMC-ABC sampler.

        Parameters
        ----------
        n_populations : int
            Number of particle populations to iterate over.
        schedule : iterable of floats
            Acceptance quantiles for particle populations.

        Returns
        -------
        A dictionary with items:
        samples : list of np.arrays
            Samples from the posterior distribution of each parameter.
        samples_history : list of lists of np.arrays
            Samples from previous populations.
        weighted_sds_history : list of lists of floats
            Weighted standard deviations from previous populations.
        """

        # initialize with rejection sampling
        result = super(SMC, self).infer(quantile=schedule[0])
        parameters = result['samples']
        weights = np.ones(self.n_samples)

        # save original priors
        orig_priors = self.parameter_nodes.copy()

        params_history = []
        weighted_sds_history = []
        try:  # Try is used, since Priors will change. Finally always reverts to original.
            for tt in range(1, n_populations):
                params_history.append(parameters)

                weights /= np.sum(weights)  # normalize weights here

                # calculate weighted standard deviations
                weighted_sds = [ np.sqrt( 2. * weighted_var(p, weights) )
                                 for p in parameters ]
                weighted_sds_history.append(weighted_sds)

                # set new prior distributions based on previous samples
                for ii, p in enumerate(self.parameter_nodes):
                    new_prior = Prior(p.name, _SMC_Distribution, parameters[ii],
                                      weighted_sds[ii], weights)
                    self.parameter_nodes[ii] = p.change_to(new_prior,
                                                           transfer_parents=False,
                                                           transfer_children=True)

                # rejection sampling with the new priors
                result = super(SMC, self).infer(quantile=schedule[tt])
                parameters = result['samples']

                # calculate new unnormalized weights for parameters
                weights_new = np.ones(self.n_samples)
                for ii in range(self.n_params):
                    weights_denom = np.sum(weights *
                                           self.parameter_nodes[ii].pdf(parameters[ii]))
                    weights_new *= orig_priors[ii].pdf(parameters[ii]) / weights_denom
                weights = weights_new

        finally:
            # revert to original priors
            self.parameter_nodes = [p.change_to(orig_priors[ii],
                                                transfer_parents=False,
                                                transfer_children=True)
                                    for ii, p in enumerate(self.parameter_nodes)]

        return {'samples': parameters,
                'samples_history': params_history,
                'weighted_sds_history': weighted_sds_history}


class _SMC_Distribution(ss.rv_continuous):
    """Distribution that samples near previous values of parameters.
    Used in SMC as priors for subsequent particle populations.
    """
    def rvs(current_params, weighted_sd, weights, random_state, size=1):
        selections = random_state.choice(np.arange(current_params.shape[0]), size=size, p=weights)
        params = current_params[selections] + \
                 ss.norm.rvs(scale=weighted_sd, size=size, random_state=random_state)
        return params

    def pdf(params, current_params, weighted_sd, weights):
        return ss.norm.pdf(params, current_params, weighted_sd)


class BolfiAcquisition(SecondDerivativeNoiseMixin, LcbAcquisition):
    pass


class AsyncBolfiAcquisition(SecondDerivativeNoiseMixin, RbfAtPendingPointsMixin, LcbAcquisition):
    pass


class BOLFI(ABCMethod):
    """ BOLFI ABC inference

    Approximates the true discrepancy function by a stochastic regression model.
    Model is fit by sampling the true discrepancy function at points decided by
    the acquisition function.

    Parameters
    ----------
    model : stochastic regression model object (eg. GPyModel)
        Model to use for approximating the discrepancy function.
    acquisition : acquisition function object (eg. AcquisitionBase derivate)
        Policy for selecting the locations where discrepancy is computed
    sync : bool
        Whether to sample sychronously or asynchronously
    bounds : list of tuples (min, max) per dimension
        The region where to estimate the posterior (box-constraint)
    client : dask Client
        Client to use for computing the discrepancy values
    n_surrogate_samples : int
        Number of points to calculate discrepancy at if 'acquisition' is not given
    """

    def __init__(self, distance_node=None, parameter_nodes=None, batch_size=10,
                 model=None, acquisition=None, sync=True,
                 bounds=None, client=None, n_surrogate_samples=10):
        super(BOLFI, self).__init__(distance_node, parameter_nodes, batch_size)
        self.n_dimensions = len(self.parameter_nodes)
        self.model = model or GPyModel(self.n_dimensions, bounds=bounds)
        self.sync = sync
        if acquisition is not None:
            self.acquisition = acquisition
        elif sync is True:
            self.acquisition = BolfiAcquisition(self.model,
                                                n_samples=n_surrogate_samples)
        else:
            self.acquisition = AsyncBolfiAcquisition(self.model,
                                                     n_samples=n_surrogate_samples)
        if client is not None:
            self.client = client
        else:
            logger.debug("{}: No dask client given, creating a local client."
                    .format(self.__class__.__name__))
            self.client = Client()
            dask.set_options(get=self.client.get)

    def infer(self, threshold=None):
        """ Bolfi inference.

        Parameters
        ----------
        see get_posterior

        Returns
        -------
        see get_posterior
        """
        self.create_surrogate_likelihood()
        return self.get_posterior(threshold)

    def create_surrogate_likelihood(self):
        """ Samples discrepancy iteratively to fit the surrogate model. """
        if self.sync is True:
            logger.info("{}: Sampling {:d} samples in batches of {:d}"
                    .format(self.__class__.__name__,
                            self.acquisition.samples_left,
                            self.batch_size))
        else:
            logger.info("{}: Sampling {:d} samples asynchronously {:d} samples in parallel"
                    .format(self.__class__.__name__,
                            self.acquisition.samples_left,
                            self.batch_size))
        futures = list()  # pending future results
        pending = list()  # pending locations matched to futures by list index
        while (not self.acquisition.finished) or (len(pending) > 0):
            next_batch_size = self._next_batch_size(len(pending))
            if next_batch_size > 0:
                pending_locations = np.atleast_2d(pending) if len(pending) > 0 else None
                new_locations = self.acquisition.acquire(next_batch_size, pending_locations)
                for location in new_locations:
                    wv_dict = {param.name: np.atleast_2d(location[i])
                               for i, param in enumerate(self.parameter_nodes)}
                    future = self.distance_node.generate(1, with_values=wv_dict)
                    futures.append(future)
                    pending.append(location)
            result, result_index, futures = wait(futures, self.client)
            location = pending.pop(result_index)
            self.model.update(location, result)

    def _next_batch_size(self, n_pending):
        """ Returns batch size for acquisition function """
        if self.sync is True and n_pending > 0:
            return 0
        return min(self.batch_size, self.acquisition.samples_left) - n_pending

    def get_posterior(self, threshold):
        """ Returns the posterior

        Parameters
        ----------
        threshold: float
            discrepancy threshold for creating the posterior

        Returns
        -------
        BolfiPosterior object
        """
        return BolfiPosterior(self.model, threshold)


