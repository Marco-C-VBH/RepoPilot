`wait_chain` crashes once there are more retries than wait strategies.

`wait_chain(wait_fixed(1), wait_fixed(2), wait_fixed(3))` produces the expected waits for the first three retries and then raises `IndexError: list index out of range` on the fourth, aborting the retry loop with a traceback instead of sleeping. A chain with a single strategy fails on the second retry for the same reason.

The documented behaviour is that the last strategy in the chain keeps being used for every further attempt.
