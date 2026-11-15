"""Bounds for request values that reach a SQL statement.

PostgreSQL refuses a text value that holds a NUL and an integer outside the
column's range, and the driver raises before the statement runs. A route that
passes such a value through answers a server error instead of a client error.
Declaring these bounds on the parameter turns the value into a 422, and no
statement is built from it.
"""

# No stored text holds a NUL.
NO_NUL_PATTERN = r"^[^\x00]*$"

# INTEGER and BIGINT columns (ids, cursors).
MAX_INT32 = 2**31 - 1
MAX_BIGINT = 2**63 - 1

# An OFFSET is a 64-bit integer.
MAX_OFFSET = MAX_BIGINT
