import logging

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin


logger = logging.getLogger(__name__)


class VerifyEdxSession(MiddlewareMixin):
    """
    Middleware that sets `site_theme` attribute to request object.
    """

    def process_request(self, request):
        if not hasattr(request, 'session'):
            # There is no session, no worries then
            self._log('The is no session, no worries then')
            return

        saved_edx_session_id = request.session.get('edx_sessionid', '') or ''

        sessionid_full = (request.COOKIES.get('sessionid', '') or '').split('|')
        if len(sessionid_full) > 2:
            sessionid = sessionid_full[1]
        else:
            sessionid = ''

        if saved_edx_session_id and sessionid != saved_edx_session_id:
            logger.warning('Edx Log-out Suspected... Logging out and redirecting to login again...')
            # User signed out in LMS
            if saved_edx_session_id:
                del request.session['edx_sessionid']
            logout(request)
            return redirect('/login')

        elif sessionid and saved_edx_session_id == '':
            # Save current edx session
            request.session['edx_sessionid'] = sessionid
