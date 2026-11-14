"""Read-only HostedRoomDriver observer embedded in isolated native entrypoints.

Never render exception text or error arguments: even truncated native messages
may contain bearer credentials. Machine messages retain class/HTTP-code chains.
"""
import functools
import sys
import time


def install_driver_observer(driver_class, emit):
    def wrap(name):
        original = getattr(driver_class, name)

        @functools.wraps(original)
        def observed(self, *args, **kwargs):
            room = task = None
            for arg in args:
                identity = getattr(arg, 'identity', arg)
                room = getattr(identity, 'room_id', None) or room
                task = getattr(identity, 'task_id', None) or task
            if name == '_drop_lease' and args:
                room = args[0]

            def receipt(phase, exc=None, result=None):
                lease = result if hasattr(result, 'lease_generation') else getattr(self, '_leases', {}).get(room)
                chain = []
                current = exc
                while current is not None and len(chain) < 4:
                    code = getattr(current, 'code', None)
                    chain.append(type(current).__name__ + (':' + str(code) if isinstance(code, int) else ''))
                    current = current.__cause__ or current.__context__
                emit(dict(timestamp=time.time(), method=name + '.' + phase,
                          room_id=room, task_id=task,
                          lease_generation=getattr(lease, 'lease_generation', None),
                          exception_class=type(exc).__name__ if exc else None,
                          message=('native_driver_' + phase + (' cause=' + '/'.join(chain) if chain else ''))[:200]))

            receipt('enter', sys.exception())
            try:
                result = original(self, *args, **kwargs)
            except BaseException as exc:
                receipt('raise', exc)
                raise
            receipt('return', result=result)
            return result
        setattr(driver_class, name, observed)

    for name in ('_mark_ambiguous', '_drop_lease', '_record_task_error', '_record_error',
                 '_renew_lease_if_needed', '_ensure_lease'):
        wrap(name)
