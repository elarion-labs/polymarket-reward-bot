from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
import inspect

print("ClobClient methods (filtered):")
methods = [m for m in dir(ClobClient) if "pos" in m.lower() or "bal" in m.lower() or "order" in m.lower() or "trade" in m.lower()]
print(sorted(methods))

print("\nOrderArgs signature:")
print(inspect.signature(OrderArgs))

print("\nOrderArgs fields (if pydantic/dataclass-like):")
print(getattr(OrderArgs, "__annotations__", None))