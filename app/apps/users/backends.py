from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from .models import User


class UsernameOrEmailBackend(ModelBackend):
    """Permite ingresar con nombre de usuario o con email (sin distinguir mayúsculas)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if not username or not password:
            return None
        username = username.strip()
        candidatos = list(User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username))[:2])
        if len(candidatos) != 1:
            # Ninguno, o email repetido en dos cuentas: se corre el hasher igual (tiempo constante)
            User().set_password(password)
            return None
        user = candidatos[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
