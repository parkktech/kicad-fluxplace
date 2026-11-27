

def test_run_parallel_serial_in_a_threaded_process_and_on_a_stuck_pool(monkeypatch):
    """w103: fork() with another Python thread alive can deadlock a worker; a stuck pool falls back to the serial answer."""
    import threading, time
    from fluxplace import pairneg as PN
    f = lambda k, cost, bx: {"v": k * 2}
    ev = threading.Event()
    t = threading.Thread(target=ev.wait); t.start()
    try:
        assert PN.run_parallel(f, [1, 2, 3, 4], 4) == {k: {"v": k * 2} for k in (1, 2, 3, 4)}
    finally:
        ev.set(); t.join()
    monkeypatch.setattr(PN, "PAIRNEG_TASK_S", 0.5)
    slow = {"first": True}
    def g(k, cost, bx):
        if k == 1 and slow["first"]:
            time.sleep(5)
        return {"v": k}
    assert PN.run_parallel(g, [1, 2, 3], 3) == {1: {"v": 1}, 2: {"v": 2}, 3: {"v": 3}}
