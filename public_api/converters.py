class ConceptDocSlugConverter:
    """Path converter restricting ``{slug}`` to ``[a-z0-9-]+``.

    ``vinta_schedule_api.urls`` builds ``DefaultRouter(use_regex_path=False)``,
    which generates ``path()``-style routes (``<converter:kwarg>``) rather than
    ``re_path()`` regexes. In that mode DRF's router prefers a viewset's
    ``lookup_value_converter`` over ``lookup_value_regex`` (the latter is only
    consulted when ``use_regex_path=True``). Registering this converter is what
    actually enforces the lowercase-alnum-hyphen charset at the URL-resolution
    layer — anything else (dots, slashes, percent-encoded traversal segments)
    never reaches the view at all.
    """

    regex = "[a-z0-9-]+"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value
