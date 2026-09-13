`retry_if_exception_type` no longer matches subclasses of the configured exception types.

`@retry(retry=retry_if_exception_type(OSError), stop=stop_after_attempt(3), wait=wait_none(), reraise=True)` gives up after the first `FileNotFoundError` instead of retrying, while raising `OSError` itself is retried as expected. The same happens with user-defined hierarchies and with tuples of types: `retry_if_exception_type((KeyError, OSError))` does not retry on `PermissionError`. Because the default retry policy is supposed to retry on any `Exception`, many decorated functions now fail on their first error unless it is a bare `Exception`.

Instances of subclasses must be treated like instances of the configured types.
