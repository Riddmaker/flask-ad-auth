#!/usr/bin/python
# coding: utf8

import sqlite3
import urllib
import urlparse
import json
import logging
import base64
import datetime
import time
import requests
import functools
from collections import namedtuple

from flask import current_app, request, abort, redirect, make_response, g
from flask import _app_ctx_stack as stack
from flask_login import LoginManager, login_user, current_user


logger = logging.getLogger(__name__)


RefreshToken = namedtuple("RefreshToken",
                          ["access_token", "refresh_token", "expires_on"])


def ad_group_required(ad_group):
    """
    This will ensure that only an user with the correct AD group
    may access the decorated view.
    """
    def decorater(func):
        @functools.wraps(func)
        def decorated_view(*args, **kwargs):
            if current_app.login_manager._login_disabled:
                return func(*args, **kwargs)
            elif not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()
            elif not current_user.is_in_group(ad_group):
                if current_app.config["AD_GROUP_FORBIDDEN_REDIRECT"]:
                    return redirect(current_app.config["AD_GROUP_FORBIDDEN_REDIRECT"])
                return abort(make_response("You dont have the necessary group to access this view", 403))
            return func(*args, **kwargs)
        return decorated_view
    return decorater


def ad_required(func):
    """
    This will ensure that only an user with the basic AD group
    may access the decorated view.
    """
    @functools.wraps(func)
    def decorated_view(*args, **kwargs):
        if current_app.login_manager._login_disabled:
            return func(*args, **kwargs)
        elif not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()
        elif current_app.config["AD_AUTH_GROUP"] and not current_user.is_in_default_group():
            if current_app.config["AD_GROUP_FORBIDDEN_REDIRECT"]:
                return redirect(current_app.config["AD_GROUP_FORBIDDEN_REDIRECT"])
            return abort(make_response("You dont have the necessary group to access this view", 403))
        return func(*args, **kwargs)
    return decorated_view


class User(object):
    def __init__(self, email, access_token, refresh_token, expires_on,
                 token_type, resource, scope, group_string=None):
        self.email = email
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_on = expires_on
        self.token_type = token_type
        self.resource = resource
        self.scope = scope
        if group_string is None:
            self.groups = []
        else:
            self.groups = filter(bool, group_string.split(";"))

    @property
    def group_string(self):
        return ";".join(self.groups)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_expired(self):
        if (self.expires_on - 10) > time.time():
            return False
        return True

    def is_in_group(self, group):
        if group in self.groups:
            return True
        else:
            logger.warning("User {} not in group {}".format(self.email, group))
            return False

    def is_in_default_group(self):
        return self.is_in_group(current_app.config["AD_AUTH_GROUP"])

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        False

    def get_id(self):
        return self.email

    @property
    def expires_in(self):
        return self.expires_on - time.time()

    def full_refresh(self):
        refresh_result = ADAuth.refresh_oauth_token(self.refresh_token)
        self.access_token = refresh_result.access_token
        self.refresh_token = refresh_result.refresh_token
        self.expires_on = int(refresh_result.expires_on)
        self.refresh_groups()
        return True

    def refresh_groups(self):
        gs = ADAuth.get_user_groups(self.access_token)
        self.groups = gs
        return self.groups

    def get_groups_named(self):
        out = []
        names = {x["id"]: x["name"] for x in ADAuth.get_all_groups(self.access_token)}
        for g in self.groups:
            out.append({"id": g, "name": names.get(g, "unknown")})
        return out


class ADAuth(LoginManager):
    def __init__(self, app=None, add_context_processor=True):
        """
        Flask extension constructor.
        """
        super(ADAuth, self).__init__(
            app=app, add_context_processor=add_context_processor)

    def init_app(self, app, add_context_processor=True):
        """
        Flask extension init method. We add our variables and
        startup code. Then we just use the init method of the parent.
        """
        app.config.setdefault("AD_SQLITE_DB", "file::memory:?cache=shared")
        app.config.setdefault("AD_APP_ID", None)
        app.config.setdefault("AD_APP_KEY", None)
        app.config.setdefault("AD_REDIRECT_URI", None)
        app.config.setdefault("AD_AUTH_URL", 'https://login.microsoftonline.com/common/oauth2/authorize')
        app.config.setdefault("AD_SQLITE_DB", 'https://login.microsoftonline.com/common/oauth2/token')
        app.config.setdefault("AD_GRAPH_URL", 'https://graph.windows.net')
        app.config.setdefault("AD_CALLBACK_PATH", '/connect/get_token')
        app.config.setdefault("AD_LOGIN_REDIRECT", '/')
        app.config.setdefault("AD_GROUP_FORBIDDEN_REDIRECT", None)
        app.config.setdefault("AD_AUTH_GROUP", None)

        if hasattr(app, 'teardown_appcontext'):
            app.teardown_appcontext(self.teardown_db)
        else:
            app.teardown_request(self.teardown_db)

        # Register Callback
        app.add_url_rule(app.config["AD_CALLBACK_PATH"], "oauth_callback",
                         self.oauth_callback)

        # Parent init call
        super(ADAuth, self).init_app(
            app=app, add_context_processor=add_context_processor)

        self.user_callback = self.load_user

    def _connect_db(self):
        """
        Connect to SQLite3 database. This will create a new user table if
        it doesnt exist.
        """
        conn = sqlite3.connect(current_app.config['AD_SQLITE_DB'])
        conn.execute("CREATE TABLE IF NOT EXISTS users ("
                     "email TEXT PRIMARY KEY, "
                     "refresh_token TEXT, "
                     "access_token TEXT, "
                     "expires_on INTEGER, "
                     "token_type TEXT, "
                     "resource TEXT, "
                     "scope TEXT,"
                     "groups TEXT);")
        conn.commit()
        return conn

    def teardown_db(self, exception):
        """
        Close Sqlite3 database connection.
        """
        ctx = stack.top
        if hasattr(ctx, 'sqlite3_db'):
            ctx.sqlite3_db.close()

    @property
    def db_connection(self):
        """
        Sqlite3 connection property. Use this to get the connection.
        It will create a reusable connection on the flask context.
        """
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'sqlite3_db'):
                ctx.sqlite3_db = self._connect_db()
            return ctx.sqlite3_db

    @property
    def sign_in_url(self):
        """
        URL you need to use to login with microsoft.
        """
        url_parts = list(urlparse.urlparse(current_app.config["AD_AUTH_URL"]))
        auth_params = {
            'response_type': 'code',
            'redirect_uri': current_app.config["AD_REDIRECT_URI"],
            'client_id': current_app.config["AD_APP_ID"]
        }
        url_parts[4] = urllib.urlencode(auth_params)
        return urlparse.urlunparse(url_parts)

    @classmethod
    def datetime_from_timestamp(cls, timestamp):
        """
        Convert unix timestamp to python datetime.
        """
        timestamp = float(timestamp)
        return datetime.datetime.utcfromtimestamp(timestamp)

    @classmethod
    def get_user_token(cls, code):
        """
        Receive OAuth Token with the code received.
        """
        token_params = {
            'grant_type': 'authorization_code',
            'redirect_uri': current_app.config["AD_REDIRECT_URI"],
            'client_id': current_app.config["AD_APP_ID"],
            'client_secret': current_app.config["AD_APP_KEY"],
            'code': code,
            'resource': current_app.config["AD_GRAPH_URL"]
        }
        res = requests.post(current_app.config["AD_TOKEN_URL"], data=token_params)
        token = res.json()
        # Decode User Info
        encoded_jwt = token["id_token"].split('.')[1]
        if len(encoded_jwt) % 4 == 2:
            encoded_jwt += '=='
        else:
            encoded_jwt += '='
        user_info = json.loads(base64.b64decode(encoded_jwt))
        # Return Important Fields
        email = user_info["upn"]
        access_token = token['access_token']
        refresh_token = token['refresh_token']
        expires_on = int(token['expires_on'])
        token_type = token['token_type']
        resource = token['resource']
        scope = token['scope']
        return User(email=email, access_token=access_token,
                    refresh_token=refresh_token, expires_on=expires_on,
                    token_type=token_type, resource=resource, scope=scope)

    @classmethod
    def refresh_oauth_token(cls, refresh_token):
        """
        Receive a new access token with the refresh token. This will also
        get a new refresh token which can be used for the next call.
        """
        refresh_params = {
            'grant_type': 'refresh_token',
            'redirect_uri': current_app.config["AD_REDIRECT_URI"],
            'client_id': current_app.config["AD_APP_ID"],
            'client_secret': current_app.config["AD_APP_KEY"],
            'refresh_token': refresh_token,
            'resource': current_app.config["AD_GRAPH_URL"]
        }
        r = requests.post(current_app.config["AD_TOKEN_URL"],
                          data=refresh_params).json()
        return RefreshToken(access_token=r["access_token"],
                            refresh_token=r["refresh_token"],
                            expires_on=r["expires_on"])

    @classmethod
    def get_all_groups(cls, access_token):
        """
        Get a List of all groups in the organisation with their name.
        """
        headers = {
            "Authorization": "Bearer {}".format(access_token),
            'Accept' : 'application/json'
        }
        params = {
            "api-version": "1.6"
        }
        url = "{}/smaxtec.com/groups".format(current_app.config["AD_GRAPH_URL"])
        r = requests.get(url, headers=headers, params=params)
        groups = []
        for g in r.json()["value"]:
            g_id = g["objectId"]
            g_name = g["displayName"]
            groups.append({"id": g_id, "name":g_name})
        return groups

    @classmethod
    def get_user_groups(cls, access_token):
        """
        Get a list with the id of all groups the user belongs to.
        """
        headers = {
            "Authorization": "Bearer {}".format(access_token),
            'Accept' : 'application/json'
        }
        params = {
            "api-version": "1.6"
        }
        body = {
            "securityEnabledOnly": False
        }
        url = "{}/me/getMemberGroups".format(current_app.config["AD_GRAPH_URL"])
        my_groups = requests.post(url, headers=headers, params=params, json=body).json()
        out = []
        for g in my_groups["value"]:
            out.append(g)
        return out

    def oauth_callback(self):
        code = request.args.get('code')
        if not code:
            logger.error("NO 'code' VALUE RECEIVED")
            return abort(400)
        user = self.get_user_token(code)
        user.refresh_groups()
        # Write to db
        self.store_user(user)
        login_user(user, remember=True) # Todo Remember me
        logger.warning("User %s logged in", user.email)
        return redirect(current_app.config["AD_LOGIN_REDIRECT"])

    def store_user(self, user):
        """
        Store user in database. This will insert or replace the user with
        given email.
        """
        c = self.db_connection.cursor()
        c.execute("INSERT OR REPLACE INTO users (email, access_token, refresh_token, expires_on, "
                  "token_type, resource, scope, groups) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (user.email, user.access_token, user.refresh_token, user.expires_on,
                   user.token_type, user.resource, user.scope, user.group_string))
        self.db_connection.commit()
        return user

    def query_user(self, email):
        """
        Query User from db. Will return the user object or None.
        """
        c = self.db_connection.cursor()
        c.execute("SELECT email, access_token, refresh_token, expires_on, "
                  "token_type, resource, scope, groups FROM users WHERE email=?", (unicode(email),))
        row = c.fetchone()
        if row:
            return User(email=row[0], access_token=row[1], refresh_token=row[2],
                        expires_on=int(row[3]), token_type=row[4], resource=row[5],
                        scope=row[6], group_string=row[7])
        return None

    def load_user(self, email):
        logger.debug("loading user %s", email)
        user = self.query_user(email)
        # User exists in db
        if user:
            # Still valid
            if not user.is_expired:
                g.user_id = user.email
                return user
            # Try to refresh with refresh token
            else:
                logger.warning("Refreshing user %s", email)
                user.full_refresh()
                self.store_user(user)
                g.user_id = user.email
                return user
        logger.warning("User %s not in database", email)
        # We need a new authentication
        # maybe reload sign_in_url automatically
        return None

