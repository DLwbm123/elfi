import numpy as np
import time
import timeit

import elfi

def get_sleep_simulator(sleep_time=.1, *args, **kwargs):
    def sim(*args, **kwargs):
        time.sleep(sleep_time)
        return np.array([[1]])
    return sim

def run_cache_test(sim, sleep_time):
    t0 = timeit.default_timer()
    a = sim.acquire(1)
    a.compute()
    td = timeit.default_timer() - t0
    assert td > sleep_time

    t0 = timeit.default_timer()
    a = sim.acquire(1)
    res = a.compute()
    td = timeit.default_timer() - t0
    assert td < sleep_time

    return res

def clear_elfi_client():
    elfi.env.client().shutdown()
    elfi.env.set(client=None)


class Test_persistence():

    def test_worker_memory_cache(self):
        sleep_time = .2
        simfn = get_sleep_simulator(sleep_time)
        sim = elfi.Simulator('sim', simfn, observed=0, store=elfi.MemoryStore())
        res = run_cache_test(sim, sleep_time)
        assert res[0][0] == 1

        # Test that nodes derived from `sim` benefit from the caching
        summ = elfi.Summary('sum', lambda x: x, sim)
        t0 = timeit.default_timer()
        res = summ.acquire(1).compute()
        td = timeit.default_timer() - t0
        assert td < sleep_time
        assert res[0][0] == 1

        clear_elfi_client()

    def test_local_object_cache(self):
        local_store = np.zeros((10,1))
        self.run_local_object_cache_test(local_store)

    def run_local_object_cache_test(self, local_store):
        sleep_time = .2
        simfn = get_sleep_simulator(sleep_time)
        sim = elfi.Simulator('sim', simfn, observed=0, store=local_store)
        run_cache_test(sim, sleep_time)
        assert local_store[0][0] == 1

        # Test that nodes derived from `sim` benefit from the storing
        summ = elfi.Summary('sum', lambda x : x, sim)
        t0 = timeit.default_timer()
        res = summ.acquire(1).compute()
        td = timeit.default_timer() - t0
        assert td < sleep_time
        assert res[0][0] == 1

        clear_elfi_client()

