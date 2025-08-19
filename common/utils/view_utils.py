from django.shortcuts import get_object_or_404

import django_virtual_models as v
from rest_framework import generics, mixins, status
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet, ViewSetMixin


class RefetchReturnInstanceAfterWriteMixin:
    def get_serializer_class(self):
        """
        Return the class to use for the serializer.
        Defaults to using `self.serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        assert (  # noqa: S101
            self.serializer_class is not None
            or getattr(self, "list_serializer_class", None) is not None
            or getattr(self, "retrieve_serializer_class", None) is not None
            or getattr(self, "read_serializer_class", None) is not None
        ), (
            f"'{self.__class__.__name__}' should either include one of `serializer_class` and "
            f"`read_serializer_class` attribute, or override one of the `get_serializer_class()`, "
            f"`get_read_serializer_class()` method."
        )

        if self.action == "list":
            return getattr(
                self,
                "list_serializer_class",
                getattr(self, "read_serializer_class", self.serializer_class),
            )

        if self.action == "retrieve":
            return getattr(
                self,
                "retrieve_serializer_class",
                getattr(self, "read_serializer_class", self.serializer_class),
            )

        return getattr(
            self,
            "retrieve_serializer_class",
            getattr(self, "read_serializer_class", self.serializer_class),
        )

    def get_list_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in list actions.
        """
        serializer_class = self.get_list_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_list_serializer_class(self):
        """
        Return the class to use for the serializer in list actions.
        Defaults to using `self.list_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        if getattr(self, "list_serializer_class", None) is None:
            return self.get_read_serializer_class()

        return self.list_serializer_class

    def get_retrieve_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in retrieve actions.
        """
        serializer_class = self.get_retrieve_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_retrieve_serializer_class(self):
        """
        Return the class to use for the serializer in retrieve actions.
        Defaults to using `self.retrieve_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        if getattr(self, "retrieve_serializer_class", None) is None:
            return self.get_read_serializer_class()

        return self.retrieve_serializer_class

    def get_create_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in create actions.
        """
        serializer_class = self.get_create_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_create_serializer_class(self):
        """
        Return the class to use for the serializer in create actions.
        Defaults to using `self.create_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins can send extra fields, others cannot)
        """
        if getattr(self, "create_serializer_class", None) is None:
            return self.get_write_serializer_class()

        return self.create_serializer_class

    def get_update_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in update actions.
        """
        serializer_class = self.get_update_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_update_serializer_class(self):
        """
        Return the class to use for the serializer in update actions.
        Defaults to using `self.update_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins can send extra fields, others cannot)
        """
        if getattr(self, "update_serializer_class", None) is None:
            return self.get_write_serializer_class()

        return self.update_serializer_class

    def get_read_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output.
        """
        serializer_class = self.get_read_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_read_serializer_class(self):
        """
        Return the class to use for the serializer.
        Defaults to using `self.read_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        if getattr(self, "read_serializer_class", None) is None:
            return self.get_serializer_class()

        return self.read_serializer_class

    def get_write_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for validating
        and deserializing input.
        """
        serializer_class = self.get_write_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_write_serializer_class(self):
        """
        Return the class to use for the serializer.
        Defaults to using `self.write_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins can send extra fields, others cannot)
        """
        if getattr(self, "write_serializer_class", None) is None:
            return self.get_serializer_class()

        return self.write_serializer_class


class NoReturnWriteMixin(RefetchReturnInstanceAfterWriteMixin):
    def create(self, request, *args, **kwargs):
        serializer = self.get_write_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_write_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(status=status.HTTP_200_OK)


class CreateModelMixin(RefetchReturnInstanceAfterWriteMixin, mixins.CreateModelMixin):
    def create(self, request, *args, **kwargs):
        serializer = self.get_create_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = serializer.instance

        # re-fetches the instance so we get annotations, prefetches, and selects
        if hasattr(self, "get_return_queryset"):
            annotated_instance = self.get_return_queryset().get(pk=instance.pk)
        else:
            annotated_instance = self.get_queryset().get(pk=instance.pk)
        return_serializer = self.get_retrieve_serializer(annotated_instance)
        headers = self.get_success_headers(return_serializer.data)
        return Response(return_serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class UpdateModelMixin(RefetchReturnInstanceAfterWriteMixin, mixins.UpdateModelMixin):
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_update_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return_serializer = self.get_retrieve_serializer(
            self.get_return_object(serializer.instance)
        )

        return Response(return_serializer.data)

    def get_return_object(self, instance):
        if hasattr(self, "get_return_queryset"):
            queryset = self.get_return_queryset()
        else:
            queryset = self.get_queryset()
        queryset = self.filter_queryset(queryset)

        # Perform the lookup filtering.
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field

        assert lookup_url_kwarg in self.kwargs, (  # noqa: S101
            f"Expected view {self.__class__.__name__} to be called with a URL keyword argument "
            f'named "{lookup_url_kwarg}". Fix your URL conf, or set the `.lookup_field` '
            f"attribute on the view correctly."
        )

        filter_kwargs = {self.lookup_field: getattr(instance, self.lookup_field)}
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj


class FilterOnlyOnListMixin:
    def filter_queryset(self, queryset):
        if self.action != "list":
            return queryset
        return super().filter_queryset(queryset)


class VintaScheduleModelViewSet(
    CreateModelMixin,
    UpdateModelMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    ModelViewSet,
):
    """
    A viewset that provides default `create()`, `retrieve()`, `update()`,
    `partial_update()`, `destroy()` and `list()` actions for vinta_schedule models.
    It refetches the instance after write operations to ensure the latest data is returned.
    """

    pass


class ReadOnlyVintaScheduleModelViewSet(
    ViewSetMixin,
    RefetchReturnInstanceAfterWriteMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that provides read-only access to vinta_schedule models.
    It does not allow creation, update, or deletion of instances.
    """

    pass


class NoCreateVintaScheduleModelViewSet(
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    UpdateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow creation of new instances.
    It only allows read and update operations.
    """

    pass


class NoUpdateVintaScheduleModelViewSet(
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow update of instances.
    It only allows read and create/destroy operations.
    """

    pass


class CreateAndReadVintaScheduleModelViewSet(
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    CreateModelMixin,
    mixins.ListModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow update of instances.
    It only allows read and create operations.
    """

    pass


class NoDetailsVintaScheduleModelViewSet(
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    UpdateModelMixin,
    CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow details of instances.
    It only allows list and create/update/destroy operations.
    """

    pass
