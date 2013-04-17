# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2012-2013
# - Martin Barisits, <martin.barisits@cern.ch>, 2013

from random import uniform, shuffle

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql.expression import and_

from rucio.core.did import list_child_dids, list_files
from rucio.common.exception import InvalidReplicationRule, InsufficientQuota, DataIdentifierNotFound, ReplicationRuleNotFound
from rucio.core.lock import get_replica_locks
from rucio.core.quota import list_account_limits, list_account_usage
from rucio.core.rse import list_rse_attributes
from rucio.core.rse_expression_parser import parse_expression
from rucio.core.transfer import submit_rse_transfer
from rucio.db import models
from rucio.db.session import read_session, transactional_session


#@transactional_session
#def attach_did_hook(parent_scope, parent_name, parent_type, child_scope, child_name, child_type, session=None):
#    """
#    The child id was added to the parent did; Replication Rules have to be evaluated accordingly.
#
#    :param parent_scope:  The scope of the parent did.
#    :param parent_name:   The name of the parent did.
#    :param parent_type:   The type of the parent did.
#    :param child_scope:   The scope of the child did.
#    :param child_name:    The name of the child did.
#    :param child_type:    The type of the child did.
#    """
#
#    session.begin(subtransactions=True)
#    try:
#        #Check if the parent did is part of a replication rule
#        dscontlocks = session.query(models.ReplicaLock).filter_by(scope=parent_scope, name=parent_name, type='DSCONT_LOCK')
#        for dscontlock in dscontlocks:
#            replication_rule = session.query(models.ReplicationRule).filter_by(id=dscontlock.id)
#            if replication_rule.grouping=='ALL':
#                # All Data to the same RSE; Decision made in dscontlock.rse_id should be repeated
#                add_replica_lock(rule_id=replication_rule.id, scope=child_scope, name=child_name, rse_id=dscontlock.rse_id, account=replication_rule.account, state=lock['state'], session=session)
#                raise NotImplemented()
#            elif replication_rule.grouping=='NONE':
#                raise NotImplemented()
#            elif replication_rule.grouping=='DATASET':
#                raise NotImplemented()
#    except:
#        pass
#    session.commit()
#
#    f
#def detach_did_hook():
#    raise NotImplemented

@transactional_session
def add_replication_rule(dids, account, copies, rse_expression, grouping, weight, lifetime, locked, subscription_id, session=None):
    """
    Adds a replication rule for every did in dids

    :param dids:             List of data identifiers.
    :param account:          Account issuing the rule.
    :param copies:           The number of replicas.
    :param rse_expression:   RSE expression which gets resolved into a list of rses.
    :param grouping:         ALL -  All files will be replicated to the same RSE.
                             DATASET - All files in the same dataset will be replicated to the same RSE.
                             NONE - Files will be completely spread over all allowed RSEs without any grouping considerations at all.
    :param weight:           Weighting scheme to be used.
    :param lifetime:         The lifetime of the replication rule.
    :type lifetime:          datetime.timedelta
    :param locked:           If the rule is locked.
    :param subscription_id:  The subscription_id, if the rule is created by a subscription.
    :param session:          The database session in use.
    :returns:                A list of created replication rule ids.
    :raises:                 InvalidReplicationRule, InsufficientQuota, InvalidRSEExpression, DataIdentifierNotFound
    """

    # 1. Resolve the rse_expression into a list of RSE-ids
    rse_ids = parse_expression(rse_expression, session=session)
    selector = RSESelector(account=account, rse_ids=rse_ids, weight=weight, copies=copies, session=session)

    transfers_to_create = []
    rule_ids = []

    for elem in dids:
        # 2. Create the replication rule
        new_rule = models.ReplicationRule(account=account, name=elem['name'], scope=elem['scope'], copies=copies, rse_expression=rse_expression, locked=locked, grouping=grouping, expires_at=lifetime, weight=weight, subscription_id=subscription_id)
        rule_id = new_rule.id
        rule_ids.append(rule_id)
        try:
            new_rule.save(session=session)
        except IntegrityError, e:
            raise InvalidReplicationRule(e.args[0])
        # 3. Apply the replication rule to create locks and return a list of transfers
        transfers_to_create = __apply_replication_rule(scope=elem['scope'], name=elem['name'], rseselector=selector, account=account, rule_id=rule_id, grouping=grouping, session=session)

    # 4. Create the transfers
    for transfer in transfers_to_create:
        #TODO: Add session variable when [RUCIO-243] is done
        submit_rse_transfer(scope=transfer['scope'], name=transfer['name'], destination_rse=transfer['rse_id'], metadata=None)
    return rule_ids


@transactional_session
def __apply_replication_rule(scope, name, rseselector, account, rule_id, grouping, session=None):
    """
    Apply a created replication rule to a did

    :param scope:        Scope of the did.
    :param name:         Name of the did.
    :param rseselector:  The RSESelector to be used.
    :param account:      The account.
    :param rule_id:      The rule_id.
    :param grouping:     The grouping to be used.
    :param session:      Session of the db.
    :returns:            List of transfers to create
    """

    containers = []    # List of Containers in the Tree [{'scope':, 'name':}]
    datasetfiles = []  # List of Datasets and their files in the Tree [{'scope':, 'name':, 'files:}]
    files = []         # Files are in the format [{'scope': ,'name':, 'size', 'replica_locks': [{'rse_id':, 'state':}]}]

    # a) Is the did a file, dataset or container
    try:
        did = session.query(models.DataIdentifier).filter_by(scope=scope, name=name, deleted=False).one()
    except NoResultFound:
        raise DataIdentifierNotFound('Data identifier %s:%s is not valid.' % (scope, name))

    # b) Resolve the did
    if did.type == 'file':
        # ########
        # # FILE #
        # ########
        # Get the file information
        datasetfiles = [{'scope': None, 'name': None, 'files': [{'scope': scope, 'name': name, 'size': did.size, 'replica_locks': get_replica_locks(scope=scope, name=name)}]}]
    elif did.type == 'dataset':
        # ###########
        # # DATASET #
        # ###########
        # Get the file information
        tmp_files = {}
        for file in list_files(scope=scope, name=name, session=session):
            tmp_files['%s:%s' % (file['scope'], file['name'])] = {'scope': file['scope'], 'name': file['name'], 'size': file['size'], 'replica_locks': []}
        # Get all the locks associated to the files
        filelocks = session.query(models.DataIdentifierAssociation, models.ReplicaLock).join(models.ReplicaLock, and_(models.DataIdentifierAssociation.child_scope == models.ReplicaLock.scope, models.DataIdentifierAssociation.child_name == models.ReplicaLock.name)).filter(models.DataIdentifierAssociation.scope == scope, models.DataIdentifierAssociation.name == name)
        #TODO: Is this an efficient query?
        for did, lock in filelocks:
            tmp_files['%s:%s' % (did['child_scope'], did['child_name'])]['replica_locks'].append({'rse_id': lock['rse_id'], 'state': lock['state']})
        datasetfiles = [{'scope': scope, 'name': name, 'files': tmp_files.values()}]
        files = tmp_files.values()
    elif did.type == 'container':
        # #############
        # # CONTAINER #
        # #############
        # Get the file, dataset and container information
        for dscont in list_child_dids(scope=scope, name=name, session=session):
            if dscont['type'] == 'container':
                containers.append({'scope': dscont['scope'], 'name': dscont['name']})
            else:  # dataset
                tmp_files = {}
                # Get the file information
                for file in list_files(scope=dscont['scope'], name=dscont['name'], session=session):
                    tmp_files['%s:%s' % (file['scope'], file['name'])] = {'scope': file['scope'], 'name': file['name'], 'size': file['size'], 'replica_locks': []}
                # Get all the locks associated to the files
                filelocks = session.query(models.DataIdentifierAssociation, models.ReplicaLock).join(models.ReplicaLock, and_(models.DataIdentifierAssociation.child_scope == models.ReplicaLock.scope, models.DataIdentifierAssociation.child_name == models.ReplicaLock.name)).filter(models.DataIdentifierAssociation.scope == dscont['scope'], models.DataIdentifierAssociation.name == dscont['name'])
                #TODO: Is this an efficient query
                for did, lock in filelocks:
                    tmp_files['%s:%s' % (did['child_scope'], did['child_name'])]['replica_locks'].append({'rse_id': lock['rse_id'], 'state': lock['state']})
                datasetfiles.append({'scope': dscont['scope'], 'name': dscont['name'], 'files': tmp_files.values()})
                files.extend(tmp_files.values())

    # c) Select the locks for the dids
    locks_to_create = []      # DB Objects
    hints_to_create = []      # DB Objects
    transfers_to_create = []  # [{'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']}]
    if grouping == 'NONE':
        # ########
        # # NONE #
        # ########
        for dataset in datasetfiles:
            for file in dataset['files']:
                rse_ids = rseselector.select_rse(file['size'], [lock['rse_id'] for lock in file['replica_locks']])
                for rse_id in rse_ids:
                    if rse_id in [lock['rse_id'] for lock in file['replica_locks']]:
                        if 'WAITING' in [lock['state'] for lock in file['replica_locks'] if lock['rse_id'] == rse_id]:
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='WAITING'))
                        else:
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='OK'))
                    else:
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='WAITING'))
                        transfers_to_create.append({'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']})
            hints_to_create.append(models.ReplicationRuleHint(scope=dataset['scope'], name=dataset['name'], rule_id=rule_id))
        for container in containers:
            hints_to_create.append(models.ReplicationRuleHint(scope=container['scope'], name=container['name'], rule_id=rule_id))
    elif grouping == 'ALL':
        # #######
        # # ALL #
        # #######
        size = sum([file['size'] for file in files])
        rse_coverage = {}  # {'rse_id': coverage }
        for file in files:
            for lock in file['replica_locks']:
                if lock['rse_id'] in rse_coverage:
                    rse_coverage[lock['rse_id']] += file['size']
                else:
                    rse_coverage[lock['rse_id']] = file['size']
        #TODO add a threshold here?
        preferred_rse_ids = [x[0] for x in sorted(rse_coverage.items(), key=lambda tup: tup[1], reverse=True)]
        rse_ids = rseselector.select_rse(size, preferred_rse_ids)
        for rse_id in rse_ids:
            for file in files:
                if rse_id in [lock['rse_id'] for lock in file['replica_locks']]:
                    if 'WAITING' in [lock['state'] for lock in file['replica_locks'] if lock['rse_id'] == rse_id]:
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='WAITING'))
                    else:
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='OK'))
                else:
                    locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='WAITING'))
                    transfers_to_create.append({'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']})
        # Create hints
        for dataset in datasetfiles:
            hints_to_create.append(models.ReplicationRuleHint(scope=dataset['scope'], name=dataset['name'], rule_id=rule_id, rse_id=rse_id))
        for container in containers:
            hints_to_create.append(models.ReplicationRuleHint(scope=container['scope'], name=container['name'], rule_id=rule_id, rse_id=rse_id))
    else:
        # ###########
        # # DATASET #
        # ###########
        for dataset in datasetfiles:
            size = sum(file['size'] for file in dataset['files'])
            rse_coverage = {}  # {'rse_id': coverage }
            for file in dataset['files']:
                for lock in file['replica_locks']:
                    if lock['rse_id'] in rse_coverage:
                        rse_coverage[lock['rse_id']] += file['size']
                    else:
                        rse_coverage[lock['rse_id']] = file['size']
            preferred_rse_ids = [x[0] for x in sorted(rse_coverage.items(), key=lambda tup: tup[1], reverse=True)]
            #TODO: Add some threshhold
            rse_ids = rseselector.select_rse(size, preferred_rse_ids)
            for rse_id in rse_ids:
                for file in dataset['files']:
                    if rse_id in [lock['rse_id'] for lock in file['replica_locks']]:
                        if 'WAITING' in [lock['state'] for lock in file['replica_locks'] if lock['rse_id'] == rse_id]:
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='WAITING'))
                        else:
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='OK'))
                    else:
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, state='WAITING'))
                        transfers_to_create.append({'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']})
            hints_to_create.append(models.ReplicationRuleHint(scope=dataset['scope'], name=dataset['name'], rule_id=rule_id, rse_id=rse_id))
        for container in containers:
            hints_to_create.append(models.ReplicationRuleHint(scope=container['scope'], name=container['name'], rule_id=rule_id))

    # d) Put the locks and hints to the DB, return the transfers
    session.add_all(locks_to_create)
    session.add_all(hints_to_create)
    session.flush()
    return(transfers_to_create)


@read_session
def list_replication_rules(filters={}, session=None):
    """
    List replication rules.

    :param filters: dictionary of attributes by which the results should be filtered.
    :param session: The database session in use.
    """

    query = session.query(models.ReplicationRule)
    if filters:
        for (k, v) in filters.items():
            query = query.filter(getattr(models.ReplicationRule, k) == v)

    for row in query.yield_per(5):
        d = {}
        for column in row.__table__.columns:
            d[column.name] = getattr(row, column.name)
        yield d


@read_session
def get_replication_rule(rule_id, session=None):
    """
    Get a specific replication rule.

    :param rule_id: The rule_id to select
    :param session: The database session in use.
    """

    try:
        query = session.query(models.ReplicationRule.filter_by(id=rule_id)).one()
        return {'id': query.id,
                'subscription_id': query.subsciption_id,
                'account': query.account,
                'scope': query.scope,
                'name': query.name,
                'state': query.state,
                'rse_expression': query.rse_expression,
                'copies': query.copies,
                'expires_at': query.expires_at,
                'weight': query.weight,
                'locked': query.locked,
                'grouping': query.grouping}
    except NoResultFound:
        raise ReplicationRuleNotFound()


class RSESelector():
    """
    Representation of the RSE selector
    """

    @read_session
    def __init__(self, account, rse_ids, weight, copies, session=None):
        """
        Initialize the RSE Selector.

        :param account:  Account owning the rule.
        :param rse_ids:  List of rse_ids.
        :param weight:   Weighting to use.
        :param copies:   Number of copies to create.
        :param session:  DB Session in use.
        :raises:         InvalidReplicationRule
        """

        self.account = account
        self.rses = []
        self.copies = copies
        if weight is not None:
            for rse_id in rse_ids:
                attributes = list_rse_attributes(rse=None, rse_id=rse_id, session=session)
                if weight not in attributes:
                    continue  # The RSE does not have the required weight set, therefore it is ignored
                try:
                    self.rses.append({'rse_id': rse_id, 'weight': float(attributes[weight])})
                except ValueError:
                    raise InvalidReplicationRule('The RSE with id \'%s\' has a non-number specified for the weight \'%s\'' % (rse_id, weight))
        else:
            self.rses = [{'rse_id': rse_id, 'weight': 1} for rse_id in rse_ids]
        if not self.rses:
            raise InvalidReplicationRule('Target RSE set empty (Check if weight attribute is set for the specified RSEs)')

        for rse in self.rses:
            #TODO: Add RSE-space-left here!
            rse['quota_left'] = list_account_limits(account=account, rse_id=rse['rse_id'], session=session) - list_account_usage(account=account, rse_id=rse['rse_id'], session=session)

        self.rses = [rse for rse in self.rses if rse['quota_left'] > 0]

    def select_rse(self, size, preferred_rse_ids):
        """
        Select n RSEs to replicate data to.

        :param size:               Size of the block being replicated.
        :param preferred_rse_ids:  Ordered list of preferred rses. (If possible replicate to them)
        :returns:                  List of RSE ids.
        :raises:                   InsufficientQuota
        """

        result = []
        for copy in range(self.copies):
            #Only use RSEs which have enough quota
            rses = [rse for rse in self.rses if rse['quota_left'] > size and rse['rse_id'] not in result]
            if not rses:
                #No site has enough quota
                raise InsufficientQuota('There is insufficient quota on any of the RSE\'s to fullfill the operation')
            #Filter the preferred RSEs to those with enough quota
            #preferred_rses = [x for x in preferred_rse_ids if x in [rse['rse_id'] for rse in rses]]
            preferred_rses = [rse for rse in rses if rse['rse_id'] in preferred_rse_ids]
            if preferred_rses:
                rse_id = self.__choose_rse(preferred_rses)
            else:
                rse_id = self.__choose_rse(rses)
            result.append(rse_id)
            self.__update_quota(rse_id, size)
        return result

    def __update_quota(self, rse_id, size):
        """
        Update the internal quota value.

        :param rse_ids:  RSE-id to update.
        :param size:     Size to substract.
        """

        for element in self.rses:
            if element['rse_id'] == rse_id:
                element['quota_left'] -= size
                return

    def __choose_rse(self, rses):
        """
        Choose an RSE based on weighting.

        :param rses:  The rses to be considered for the choose.
        :return:      The id of the chosen rse
        """

        shuffle(rses)
        pick = uniform(0, sum([rse['weight'] for rse in rses]))
        weight = 0
        for rse in rses:
            weight += rse['weight']
            if pick <= weight:
                return rse['rse_id']
