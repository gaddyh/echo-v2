"""Echo v2 domain layer.

Domain entities and value objects that are provider-neutral and
user-scoped. The provider boundary (``echo_v2.ports``) emits *provider
events* carrying a :class:`ConnectionRef`; the domain layer resolves the
connection to a user and produces *domain events* carrying a ``user_id``.
"""
