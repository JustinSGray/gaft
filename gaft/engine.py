#!/usr/bin/env python
# -*- coding: utf-8 -*-

''' Genetic Algorithm engine definition '''

import logging
import math
from functools import wraps

from .components import GAIndividual, GAPopulation
from .plugin_interfaces.operators import GASelection, GACrossover, GAMutation
from .plugin_interfaces.analysis import OnTheFlyAnalysis
from .mpiutil import mpi


class GAEngine(object):
    '''
    Class for representing a Genetic Algorithm engine.
    '''
    def __init__(self, population, selection, crossover, mutation,
                 fitness=None, analysis=None):
        '''
        The Genetic Algorithm engine class is the central object in GAPY framework
        for running a genetic algorithm optimization. Once the population with
        individuals,  a set of genetic operators and fitness function are setup,
        the engine object unites these informations and provide means for running
        a genetic algorthm optimization.

        :param population: The GAPopulation to be reproduced in evolution iteration.
        :param selection: The GASelection to be used for individual seleciton.
        :param crossover: The GACrossover to be used for individual crossover.
        :param mutation: The GAMutation to be used for individual mutation.
        :param fitness: The fitness calculation function for an individual in population.

        :param analysis: All analysis class for on-the-fly analysis.
        :type analysis: list of OnTheFlyAnalysis subclasses.
        '''
        # Set logger.
        logger_name = 'gaft.{}'.format(self.__class__.__name__)
        self.logger = logging.getLogger(logger_name)

        # Attributes assignment.
        self.population = population
        self.fitness = fitness
        self.selection= selection
        self.crossover= crossover
        self.mutation= mutation
        self.analysis = [] if analysis is None else [a() for a in analysis]

        # Maxima and minima in population.
        self.fmax, self.fmin, self.fmean = None, None, None

        # Default fitness functions.
        self.ori_fitness, self.fitness = None, None

        # Store current generation number.
        self.current_generation = -1  # Starts from 0.

        # Check parameters validity.
        self._check_parameters()

    def run(self, ng=100):
        '''
        Run the Genetic Algorithm optimization iteration with specified parameters.

        :param control_parameters: An instance of GAControlParamters specifying
                                   number of evolution generations etc.
        '''
        if self.fitness is None:
            raise AttributeError('No fitness function in GA engine')

        # Get the maxima and minima in population for fitness scaling.
        self.fmax = self.population.max(self.ori_fitness)
        self.fmin = self.population.min(self.ori_fitness)
        self.fmean = self.population.mean(self.ori_fitness)

        # Setup analysis objects.
        for a in self.analysis:
            a.setup(ng=ng, engine=self)

        # Enter evolution iteration.
        try:
            for g in range(ng):
                self.current_generation = g
                # The best individual in current population. 
                best_indv = self.population.best_indv(self.fitness)
                # Scatter jobs to all processes.
                local_indvs = []
                # NOTE: One series of genetic operation generates 2 new individuals.
                local_size = mpi.split_size(self.population.size // 2)

                # Fill the new population.
                for _ in range(local_size):
                    # Select father and mother.
                    parents = self.selection.select(self.population, fitness=self.fitness)
                    # Crossover.
                    children = self.crossover.cross(*parents)
                    # Mutation.
                    children = [self.mutation.mutate(child, self) for child in children]
                    # Collect children.
                    local_indvs.extend(children)

                # Gather individuals from all processes.
                indvs = mpi.merge_seq(local_indvs)
                # Retain the previous best individual.
                indvs[0] = best_indv
                # The next generation.
                self.population.individuals = indvs

                # Update population maxima and minima.
                self.fmax = self.population.max(self.ori_fitness)
                self.fmin = self.population.min(self.ori_fitness)
                self.fmean = self.population.mean(self.ori_fitness)

                # Run all analysis if needed.
                for a in self.analysis:
                    if g % a.interval == 0:
                        a.register_step(g=g, population=self.population, engine=self)
        except Exception as e:
            # Log exception info.
            if mpi.is_master:
                msg = '{} exception is catched'.format(type(e).__name__)
                self.logger.exception(msg)
            raise e
        finally:
            # Recover current generation number.
            self.current_generation = -1
            # Perform the analysis post processing.
            for a in self.analysis:
                a.finalize(population=self.population, engine=self)

    def _check_parameters(self):
        '''
        Helper function to check parameters of engine.
        '''
        if not isinstance(self.population, GAPopulation):
            raise TypeError('population must be a GAPopulation object')
        if not isinstance(self.selection, GASelection):
            raise TypeError('selection operator must be a GASelection instance')
        if not isinstance(self.crossover, GACrossover):
            raise TypeError('crossover operator must be a GACrossover instance')
        if not isinstance(self.mutation, GAMutation):
            raise TypeError('mutation operator must be a GAMutation instance')

        for ap in self.analysis:
            if not isinstance(ap, OnTheFlyAnalysis):
                msg = '{} is not subclass of OnTheFlyAnalysis'.format(ap.__name__)
                raise TypeError(msg)

    # Decorators.

    def fitness_register(self, fn):
        '''
        A decorator for fitness function register.
        '''
        @wraps(fn)
        def _fn_with_fitness_check(indv):
            '''
            A wrapper function for fitness function with fitness value check.
            '''
            # Check indv type.
            if not isinstance(indv, GAIndividual):
                raise TypeError('indv must be a GAIndividual object')

            # Check fitness.
            fitness = fn(indv)
            is_invalid = (type(fitness) is not float) or (math.isnan(fitness))
            if is_invalid:
                msg = 'Fitness value(value: {}, type: {}) is invalid'
                msg = msg.format(fitness, type(fitness))
                raise ValueError(msg)
            return fitness

        self.fitness = _fn_with_fitness_check
        if self.ori_fitness is None:
            self.ori_fitness = _fn_with_fitness_check

    def analysis_register(self, analysis_cls):
        '''
        A decorator for analysis regsiter.
        '''
        if not issubclass(analysis_cls, OnTheFlyAnalysis):
            raise TypeError('analysis class must be subclass of OnTheFlyAnalysis')

        # Add analysis instance to engine.
        analysis = analysis_cls()
        self.analysis.append(analysis)

    # Functions for fitness scaling.

    def linear_scaling(self, target='max', ksi=0.5):
        '''
        A decorator constructor for fitness function linear scaling.

        :param target: The optimization target, maximization or minimization.
        :type target: str, 'max' or 'min'

        :param ksi: Selective pressure adjustment value.
        :type ksi: float

        Linear Scaling:
            1. arg max f(x), then f' = f - min{f(x)} + ksi;
            2. arg min f(x), then f' = max{f(x)} - f(x) + ksi;
        '''
        def _linear_scaling(fn):
            # For original fitness calculation.
            self.ori_fitness = fn

            @wraps(fn)
            def _fn_with_linear_scaling(indv):
                # Original fitness value.
                f = fn(indv)
                # Determine the value of a and b.
                if target == 'max':
                    f_prime = f - self.fmin + ksi
                elif target == 'min':
                    f_prime = self.fmax - f + ksi
                else:
                    raise ValueError('Invalid target type({})'.format(target))
                return f_prime

            return _fn_with_linear_scaling

        return _linear_scaling

    def dynamic_linear_scaling(self, target='max', ksi0=2, r=0.9):
        '''
        A decorator constructor for fitness dynamic linear scaling.

        :param target: The optimization target, maximization or minimization.
        :type target: str, 'max' or 'min'

        :param ksi0: Initial selective pressure adjustment value, default value
                     is 2
        :type ksi0: float

        :param r: The reduction factor for selective pressure adjustment value,
                  ksi^(k-1)*r is the adjustment value for generation k, default
                  value is 0.9
        :type r: float in range [0.9, 0.999]

        Dynamic Linear Scaling:
            For maximizaiton, f' = f(x) - min{f(x)} + ksi^k, k is generation number.
        '''
        def _dynamic_linear_scaling(fn):
            # For original fitness calculation.
            self.ori_fitness = fn

            @wraps(fn)
            def _fn_with_dynamic_linear_scaling(indv):
                f = fn(indv)
                k = self.current_generation + 1

                if target == 'max':
                    f_prime = f - self.fmin + ksi0*(r**k)
                elif target == 'min':
                    f_prime = self.fmax - f + ksi0*(r**k)
                else:
                    raise ValueError('Invalid target type({})'.format(target))
                return f_prime

            return _fn_with_dynamic_linear_scaling

        return _dynamic_linear_scaling

