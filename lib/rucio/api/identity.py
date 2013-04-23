# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2012
# - Mario Lassnig, <mario.lassnig@cern.ch>, 2012

from rucio.api import permission
from rucio.common import exception
from rucio.core import identity


def add_identity(identity_key, type, password=None):
    """
    Creates a user identity.

    :param identity_key: The identity key name. For example x509 DN, or a username.
    :param type: The type of the authentication (x509, gss, userpass)
    :param password: If type==userpass, this sets the password.
    """
    return identity.add_identity(identity_key, type, password)


def del_identity(identity_key, type):
    """
    Deletes a user identity.

    :param identity_key: The identity key name. For example x509 DN, or a username.
    :param type: The type of the authentication (x509, gss, userpass).
    """
    return identity.del_identity(identity_key, type)


def add_account_identity(identity_key, type, account, issuer, default=False):
    """
    Adds a membership association between identity and account.

    :param identity_key: The identity key name. For example x509 DN, or a username.
    :param type: The type of the authentication (x509, gss, userpass).
    :param account: The account name.
    :param issuer: The issuer account.
    :param default: If True, the account should be used by default with the provided identity.
    """
    kwargs = {'identity': identity_key, 'type': type, 'account': account}
    if not permission.has_permission(issuer=issuer, action='add_account_identity', kwargs=kwargs):
            raise exception.AccessDenied('Account %s can not identity' % (issuer))

    return identity.add_account_identity(identity_key, type, account, default)


def del_account_identity(identity_key, type, account):
    """
    Removes a membership association between identity and account.

    :param identity_key: The identity key name. For example x509 DN, or a username.
    :param type: The type of the authentication (x509, gss, userpass).
    :param account: The account name.
    """
    return identity.del_account_identity(identity_key, type, account)


def list_identities(**kwargs):
    """
    Returns a list of all enabled identities.

    returns: A list of all enabled identities.
    """
    return identity.list_identities(**kwargs)
