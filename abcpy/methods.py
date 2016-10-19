"""
These are sketches of how to use the ABC graphical model in the algorithms
"""
import numpy as np


class ABCMethod(object):
    def __init__(self, N, distance_node=None, parameter_nodes=None, batch_size=10):

        if not distance_node or not parameter_nodes:
            raise ValueError("Need to give the distance node and list of parameter nodes")

        self.N = N
        self.distance_node = distance_node
        self.parameter_nodes = parameter_nodes
        self.batch_size = batch_size

    def infer(self, spec, *args, **kwargs):
        raise NotImplementedError


class Rejection(ABCMethod):
    """
    Rejection sampler.
    """
    def infer(self, threshold):
        """
        Run the rejection sampler. Inference can be repeated with a different
        threshold without rerunning the simulator.
        """

        # only run at first call
        if not hasattr(self, 'distances'):
            self.distances = self.distance_node.generate(self.N, batch_size=self.batch_size).compute()
            self.parameters = [p.acquire(self.N).compute()
                               for p in self.parameter_nodes]

        accepted = self.distances < threshold
        posteriors = [p[accepted] for p in self.parameters]

        return posteriors


class BOLFI(ABCMethod):

    def infer(self, spec, parameters=None, distance=None, threshold=None):

        lik = GPLikelihoodApproximation().construct(parameters, distance)

        # TODO
        # - Construct PyMC model here using the lik
        # - Run the MCMC

        # Fixme: return the actual sample
        return lik


class GPLikelihoodApproximation():

    def construct(self, parameters=None, distance=None):

        while not self.GP.is_finished():
            values = self.acquisition.acquire()
            # Map the parameter values for the nodes
            values_hash = {param.name: values[:,i] for i, param in enumerate(parameters)}
            distances = distance.generate(len(values), self.batch_size, with_values=values_hash).compute()
            self.GP.update(parameters, distances)

        return self.GP












# class SyntheticLikelihood(ABCMethod):
#
#     def create_objective(self, model, parameters=None, summaries=None, **kwargs):
#         """
#
#         Parameters
#         ----------
#         model
#         parameter
#            array of nodes
#         summaries
#            array of nodes
#         kwargs
#
#         Returns
#         -------
#
#         """
#
#         parameter_values = []
#
#         for p in parameters:
#             values = Values()
#             values.replace(p, parents=False)
#             parameter_values.append(values)
#
#         def objective(params):
#             S = np.zeros([self.N, len(summaries)])
#             y = np.zeros([1, len(summaries)])
#             for i, s in enumerate(summaries):
#                 parameter_values[i].values[0:self.N] = params[i]
#                 S[:, i] = s.generate(self.N)
#                 y[i] = s.observed
#             cov = np.cov(S, rowvar=False)
#             mean = np.mean(S, axis=0)
#
#             lik = stats.multivariate_normal.pdf(y, mean, cov)
#             return lik
#
#         return objective
