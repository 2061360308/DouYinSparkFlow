class ServiceError(Exception):
    pass


class NotFound(ServiceError):
    pass


class Conflict(ServiceError):
    pass


class ValidationError(ServiceError):
    pass
