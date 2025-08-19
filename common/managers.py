from collections.abc import Iterable


def requires_annotation(*annotation_names: Iterable[str]):
    """
    Decorator to enforce the function requires a specific annotation.

    Args:
        annotation_name: The name of the required annotation
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            queryset = args[0]

            # Check if the annotation exists in the QuerySet's annotations
            for annotation_name in annotation_names:
                if annotation_name not in getattr(queryset.query, "annotations", {}):
                    raise ValueError(
                        f"Annotation '{annotation_name}' is required for this function."
                    )
            return func(*args, **kwargs)

        return wrapper

    return decorator
