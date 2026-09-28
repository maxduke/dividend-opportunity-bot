# Isolate provider calls in disposable processes

An asyncio timeout cannot stop a blocking Python thread. Four permanently
blocked calls exhausted the shared executor and prevented healthy fallback
providers from running. Runtime AKShare calls now use disposable Python
processes with at most four admitted calls per event loop. Queueing, startup,
and execution share a timeout; cancellation or timeout kills and reaps the
child before releasing its slot. The asynchronous calendar refresh uses the
same mechanism.

The parent sends a trusted module/function reference and arguments over stdin;
results, including pandas metadata, return over stdout. Provider output is
redirected away from that channel and exception strings are not forwarded,
since they may include authenticated URLs. This is local trusted IPC, not an
interface for untrusted serialized data.

A worker installs the proxy patch before importing the requested provider only
when the parent has already enabled the patch. Existing parent-side balance
checks and paid-call routing remain responsible for admission; workers do not
add balance requests or retries. Credentials remain in the deployment
environment, never in command arguments.

The tradeoff is interpreter/library startup overhead on each cache miss. Daily
history and valuation caches continue to amortize provider calls. Disposable
processes are preferred over a persistent pool so recovery does not depend on
replacing a pool containing stuck calls.
