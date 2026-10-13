"""
Cliente de Anura.

- Originar llamadas: API Click2Dial (kb.anura.com.ar/es/articles/3250664):
  POST https://api.anura.com.ar/adapter/default/click2call  (Authorization: Bearer <token>,
  form: called, extension, customs[1..6]). Primero suena la terminal principal de la extensión y,
  al atender, se disca el destino. El token se genera en el panel: Integraciones → Click 2 Dial.
- Cortar por API NO está disponible (Anura confirmó que el Tenant CallManager no se puede usar):
  el agente corta desde su teléfono.
- Eventos: los manda Anura por WebHooks templetizados (Configuración → Eventos) — ver
  `plantilla_webhook()` en services.py.
- API de tenant (CDRs, bloqueados, grabaciones por callId): opcional, solo si Anura la habilita.
"""
import logging
import time
import uuid

import requests

logger = logging.getLogger('apps.telefonia')


class ErrorAnura(Exception):
    pass


class ClienteAnura:
    def __init__(self, config):
        self.config = config
        self.base = (config.api_url or '').rstrip('/')

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _headers(self):
        h = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        c = self.config
        if c.auth_tipo == c.AUTH_BEARER and c.api_token:
            h['Authorization'] = f'Bearer {c.api_token}'
        elif c.auth_tipo == c.AUTH_HEADER and c.api_token:
            h[c.auth_header_nombre or 'X-Api-Key'] = c.api_token
        return h

    def _auth(self):
        c = self.config
        return (c.api_usuario, c.api_password) if c.auth_tipo == c.AUTH_BASIC else None

    def _req(self, metodo, path, **kwargs):
        if not self.base:
            raise ErrorAnura('Falta configurar la URL de la API de Anura.')
        url = f'{self.base}{path}'
        inicio = time.monotonic()
        try:
            resp = requests.request(metodo, url, headers=self._headers(), auth=self._auth(),
                                    timeout=kwargs.pop('timeout', 15), **kwargs)
        except requests.RequestException as e:
            raise ErrorAnura(f'No se pudo conectar con Anura: {e}') from e
        finally:
            logger.info('Anura %s %s (%d ms)', metodo, path, int((time.monotonic() - inicio) * 1000))
        if resp.status_code >= 300:
            raise ErrorAnura(f'Anura respondió {resp.status_code}: {resp.text[:300]}')
        try:
            return resp.json()
        except ValueError:
            return {'raw': resp.text}

    # ── Operaciones ───────────────────────────────────────────────────────
    def dial(self, extension: str, destino: str, custom: str = '') -> dict:
        """Click2Dial: suena la terminal principal de `extension` y, al atender, se disca `destino`."""
        c = self.config
        if not c.click2dial_token:
            raise ErrorAnura('Falta el token de Click2Dial (panel de Anura: Integraciones → Click 2 Dial).')
        datos = [('called', destino.lstrip('+')), ('extension', extension)]
        if custom:
            datos.append(('customs', custom))  # vuelve en los eventos como {{ custom1 }}
        inicio = time.monotonic()
        try:
            resp = requests.post(c.click2dial_url, data=datos, timeout=15,
                                 headers={'Authorization': f'Bearer {c.click2dial_token}'})
        except requests.RequestException as e:
            raise ErrorAnura(f'No se pudo conectar con Anura: {e}') from e
        finally:
            logger.info('Anura click2dial ext=%s (%d ms)', extension, int((time.monotonic() - inicio) * 1000))
        if resp.status_code >= 300:
            raise ErrorAnura(_error_click2dial(resp))
        try:
            data = resp.json()
        except ValueError:
            data = {'raw': resp.text[:500]}
        data = data if isinstance(data, dict) else {'raw': data}
        return {'call_id': str(data.get('cdrid') or data.get('callId') or data.get('id') or ''),
                'uuid': str(data.get('uuid') or ''), 'raw': data}

    def verificar_token(self):
        """Valida el token sin llamar a nadie: con una extensión inexistente Anura responde
        401 si el token es inválido y 400 ("No Originate terminal") si el token es válido."""
        c = self.config
        try:
            resp = requests.post(c.click2dial_url, data={'called': '0', 'extension': '__crm_prueba__'}, timeout=15,
                                 headers={'Authorization': f'Bearer {c.click2dial_token}'})
        except requests.RequestException as e:
            raise ErrorAnura(f'No se pudo conectar con Anura: {e}') from e
        if resp.status_code == 401:
            raise ErrorAnura('Anura rechazó el token de Click2Dial (401). Revisalo en Integraciones → Click 2 Dial.')
        return True

    def hangup(self, terminal: str, call_uuid: str) -> dict:
        raise ErrorAnura('Anura no permite cortar llamadas por API: cortá desde tu teléfono o softphone.')

    def url_grabacion(self, call_id: str) -> str:
        data = self._req('GET', f'/tenants/recording/{call_id}/url')
        if isinstance(data, dict):
            return data.get('url') or data.get('recordingUrl') or data.get('raw', '')
        return str(data)

    def descargar(self, url: str) -> bytes:
        # Link de {{ audio_file_mp3 }}: primero sin credenciales; si lo rechaza, con las de la API de tenant.
        resp = requests.get(url, timeout=120)
        if resp.status_code in (401, 403) and self.base:
            resp = requests.get(url, headers=self._headers(), auth=self._auth(), timeout=120)
        if resp.status_code >= 300:
            raise ErrorAnura(f'No se pudo descargar la grabación ({resp.status_code})')
        return resp.content

    def cdrs(self, desde, hasta) -> list:
        params = {'from': int(desde.timestamp() * 1000), 'to': int(hasta.timestamp() * 1000)}
        if self.config.account_id:
            params['accountId'] = self.config.account_id
        data = self._req('GET', '/tenants/stats/cdrs', params=params, timeout=60)
        if isinstance(data, list):
            return data
        return data.get('cdrs') or data.get('items') or data.get('data') or []

    def numeros_bloqueados(self) -> set:
        data = self._req('GET', '/accounts/blocked-numbers')
        items = data if isinstance(data, list) else (data.get('items') or data.get('data') or [])
        numeros = set()
        for it in items:
            numeros.add(str(it.get('number') or it.get('numero') or it) if isinstance(it, dict) else str(it))
        return numeros

    def estado_colas(self):
        return self._req('GET', '/accounts/queue')

    def configurar_callhook(self, webhook_url: str) -> dict:
        """Registra el CallHook + Event Template para que Anura nos empuje los eventos de llamadas."""
        if not self.config.account_id:
            raise ErrorAnura('Falta el Account ID de Anura.')
        template = self._req('POST', '/tenants/config/events/templates', json={
            'name': 'CRM Ventas',
            'fields': ['callId', 'direction', 'status', 'calling', 'called', 'accountId', 'queueId', 'dialTime',
                       'billSeconds', 'wasRecorded', 'terminal', 'event'],
        })
        hook = self._req('POST', f'/tenants/config/accounts/{self.config.account_id}/callhooks', json={
            'url': webhook_url, 'method': 'POST',
            'events': ['START', 'ANSWER', 'END', 'NOANSWER'],
            'templateId': template.get('id') if isinstance(template, dict) else None,
        })
        return {'template': template, 'callhook': hook}


def _error_click2dial(resp):
    try:
        detalle = resp.json()
        detalle = detalle.get('message') or detalle.get('error') or str(detalle) if isinstance(detalle, dict) else str(detalle)
    except ValueError:
        detalle = resp.text[:200]
    if 'not registered' in detalle.lower():
        return 'Tu teléfono de Anura no está conectado (terminal no registrada). Abrí el softphone y reintentá.'
    if 'no originate terminal' in detalle.lower():
        return 'Tu extensión no tiene una terminal principal en Anura. Pedile a Sistemas que la configure.'
    if resp.status_code == 401:
        return 'Anura rechazó el token de Click2Dial. Revisalo en Configuración → Telefonía Anura.'
    return f'Anura respondió {resp.status_code}: {detalle}'


class ClienteAnuraDemo:
    """Simula Anura: genera los mismos eventos que mandaría el CallHook, por el mismo pipeline."""

    def __init__(self, config):
        self.config = config

    def dial(self, extension, destino, custom=''):
        call_id = f'demo-{uuid.uuid4().hex[:12]}'
        from .tasks import evento_demo
        evento_demo.apply_async(args=[call_id, 'ANSWER_START'], countdown=3)
        return {'call_id': call_id, 'uuid': call_id, 'raw': {'demo': True}}

    def verificar_token(self):
        return True

    def hangup(self, terminal, call_uuid):
        from .tasks import evento_demo
        evento_demo.delay(call_uuid, 'END')
        return {'demo': True}

    def url_grabacion(self, call_id):
        return ''

    def descargar(self, url):
        return b''

    def cdrs(self, desde, hasta):
        return []

    def numeros_bloqueados(self):
        return set()

    def configurar_callhook(self, webhook_url):
        return {'demo': True, 'url': webhook_url}


def get_cliente(config=None):
    from .models import ConfigAnura
    config = config or ConfigAnura.get()
    return ClienteAnuraDemo(config) if config.modo_demo else ClienteAnura(config)
