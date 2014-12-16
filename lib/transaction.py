"""
Construct, sign and broadcast Bitcoin transactions.
"""

import os
import sys
import binascii
import json
import hashlib
import re
import time
import decimal
import logging

import requests
from pycoin.encoding import is_sec_compressed, EncodingError
from Crypto.Cipher import ARC4
from bitcoin.core.script import CScript
from bitcoin.core import x
from bitcoin.core.key import CPubKey

from . import (config, exceptions, util, blockchain, script, backend)

class InputError (Exception):
    pass

# Constants
OP_RETURN = b'\x6a'
OP_PUSHDATA1 = b'\x4c'
OP_DUP = b'\x76'
OP_HASH160 = b'\xa9'
OP_EQUALVERIFY = b'\x88'
OP_CHECKSIG = b'\xac'
OP_1 = b'\x51'
OP_2 = b'\x52'
OP_3 = b'\x53'
OP_CHECKMULTISIG = b'\xae'

D = decimal.Decimal

def print_coin(coin):
    return 'amount: {}; txid: {}; vout: {}; confirmations: {}'.format(coin['amount'], coin['txid'], coin['vout'], coin.get('confirmations', '?')) # simplify and make deterministic

def var_int (i):
    if i < 0xfd:
        return (i).to_bytes(1, byteorder='little')
    elif i <= 0xffff:
        return b'\xfd' + (i).to_bytes(2, byteorder='little')
    elif i <= 0xffffffff:
        return b'\xfe' + (i).to_bytes(4, byteorder='little')
    else:
        return b'\xff' + (i).to_bytes(8, byteorder='little')

def op_push (i):
    if i < 0x4c:
        return (i).to_bytes(1, byteorder='little')              # Push i bytes.
    elif i <= 0xff:
        return b'\x4c' + (i).to_bytes(1, byteorder='little')    # OP_PUSHDATA1
    elif i <= 0xffff:
        return b'\x4d' + (i).to_bytes(2, byteorder='little')    # OP_PUSHDATA2
    else:
        return b'\x4e' + (i).to_bytes(4, byteorder='little')    # OP_PUSHDATA4


def get_multisig_script(address):

    # Unpack multi‐sig address.
    signatures_required, pubkeys, signatures_possible = util.extract_array(address)

    # Required signatures.
    if signatures_required == 1:
        op_required = OP_1
    elif signatures_required == 2:
        op_required = OP_2
    elif signatures_required == 3:
        op_required = OP_3
    else:
        raise InputError('Required signatures must be 1, 2 or 3.')

    # Required signatures.
    if signatures_possible == 1:
        op_total = OP_1
    elif signatures_possible == 2:
        op_total = OP_2
    elif signatures_possible == 3:
        op_total = OP_3
    else:
        raise InputError('Total possible signatures must be 1, 2 or 3.')

    # Construct script.
    script = op_required                                # Required signatures
    for public_key in pubkeys:
        public_key = binascii.unhexlify(public_key)
        script += op_push(len(public_key))              # Push bytes of public key
        script += public_key                            # Data chunk (fake) public key
    script += op_total                                  # Total signatures
    script += OP_CHECKMULTISIG                          # OP_CHECKMULTISIG

    return script

def get_monosig_script(address):

    # Construct script.
    pubkeyhash = util.base58_check_decode(address, config.ADDRESSVERSION)
    script = OP_DUP                                     # OP_DUP
    script += OP_HASH160                                # OP_HASH160
    script += op_push(20)                               # Push 0x14 bytes
    script += pubkeyhash                                # pubKeyHash
    script += OP_EQUALVERIFY                            # OP_EQUALVERIFY
    script += OP_CHECKSIG                               # OP_CHECKSIG

    return script

def make_fully_valid(pubkey):
    assert len(pubkey) == 31    # One sign byte and one nonce byte required (for 33 bytes).

    cpubkey = CPubKey(b'')
    random_bytes = hashlib.sha256(pubkey).digest()      # Deterministically generated, for unit tests.
    sign = (random_bytes[0] & 0b1) + 2                  # 0x02 or 0x03
    nonce = initial_nonce = random_bytes[1]

    while not cpubkey.is_fullyvalid:
        # Increment nonce.
        nonce += 1
        assert nonce != initial_nonce

        # Construct a possibly fully valid public key.
        possibly_fully_valid_pubkey = bytes([sign]) + pubkey + bytes([nonce % 256])
        cpubkey = CPubKey(possibly_fully_valid_pubkey)

    fully_valid_pubkey = possibly_fully_valid_pubkey
    assert len(fully_valid_pubkey) == 33
    return fully_valid_pubkey


def serialise (block_index, encoding, inputs, destination_outputs, data_output=None, change_output=None, dust_return_public_key=None):
    s  = (1).to_bytes(4, byteorder='little')                # Version

    # Number of inputs.
    s += var_int(int(len(inputs)))

    # List of Inputs.
    for i in range(len(inputs)):
        txin = inputs[i]
        s += binascii.unhexlify(bytes(txin['txid'], 'utf-8'))[::-1]         # TxOutHash
        s += txin['vout'].to_bytes(4, byteorder='little')   # TxOutIndex

        script = binascii.unhexlify(bytes(txin['scriptPubKey'], 'utf-8'))
        s += var_int(int(len(script)))                      # Script length
        s += script                                         # Script
        s += b'\xff' * 4                                    # Sequence

    # Number of outputs.
    n = 0
    n += len(destination_outputs)
    if data_output:
        data_array, value = data_output
        for data_chunk in data_array: n += 1
    else:
        data_array = []
    if change_output: n += 1
    s += var_int(n)

    # Destination output.
    for destination, value in destination_outputs:
        s += value.to_bytes(8, byteorder='little')          # Value

        if util.is_multisig(destination):
            script = get_multisig_script(destination)
        else:
            script = get_monosig_script(destination)

        s += var_int(int(len(script)))                      # Script length
        s += script

    # Data output.
    for data_chunk in data_array:
        data_array, value = data_output
        s += value.to_bytes(8, byteorder='little')        # Value

        if util.enabled('multisig_addresses', block_index):   # Protocol change.
            data_chunk = config.PREFIX + data_chunk

        # Initialise encryption key (once per output).
        key = ARC4.new(binascii.unhexlify(inputs[0]['txid']))  # Arbitrary, easy‐to‐find, unique key.

        if encoding == 'multisig':
            # Get data (fake) public key.
            if util.enabled('multisig_addresses', block_index):   # Protocol change.
                pad_length = (33 * 2) - 1 - 2 - 2 - len(data_chunk)
                assert pad_length >= 0
                data_chunk = bytes([len(data_chunk)]) + data_chunk + (pad_length * b'\x00')
                data_chunk = key.encrypt(data_chunk)
                data_pubkey_1 = make_fully_valid(data_chunk[:31])
                data_pubkey_2 = make_fully_valid(data_chunk[31:])

                # Construct script.
                script = OP_1                                   # OP_1
                script += op_push(33)                           # Push bytes of data chunk (fake) public key    (1/2)
                script += data_pubkey_1                         # (Fake) public key                  (1/2)
                script += op_push(33)                           # Push bytes of data chunk (fake) public key    (2/2)
                script += data_pubkey_2                         # (Fake) public key                  (2/2)
                script += op_push(len(dust_return_public_key))  # Push bytes of source public key
                script += dust_return_public_key                       # Source public key
                script += OP_3                                  # OP_3
                script += OP_CHECKMULTISIG                      # OP_CHECKMULTISIG
            else:
                pad_length = 33 - 1 - len(data_chunk)
                assert pad_length >= 0
                data_chunk = bytes([len(data_chunk)]) + data_chunk + (pad_length * b'\x00')
                # Construct script.
                script = OP_1                                   # OP_1
                script += op_push(len(dust_return_public_key))  # Push bytes of source public key
                script += dust_return_public_key                       # Source public key
                script += op_push(len(data_chunk))              # Push bytes of data chunk (fake) public key
                script += data_chunk                            # (Fake) public key
                script += OP_2                                  # OP_2
                script += OP_CHECKMULTISIG                      # OP_CHECKMULTISIG
        elif encoding == 'opreturn':
            if util.enabled('multisig_addresses', block_index):   # Protocol change.
                data_chunk = key.encrypt(data_chunk)
            script = OP_RETURN                                  # OP_RETURN
            script += op_push(len(data_chunk))                  # Push bytes of data chunk (NOTE: OP_SMALLDATA?)
            script += data_chunk                                # Data
        elif encoding == 'pubkeyhash':
            pad_length = 20 - 1 - len(data_chunk)
            assert pad_length >= 0
            data_chunk = bytes([len(data_chunk)]) + data_chunk + (pad_length * b'\x00')
            data_chunk = key.encrypt(data_chunk)
            # Construct script.
            script = OP_DUP                                     # OP_DUP
            script += OP_HASH160                                # OP_HASH160
            script += op_push(20)                               # Push 0x14 bytes
            script += data_chunk                                # (Fake) pubKeyHash
            script += OP_EQUALVERIFY                            # OP_EQUALVERIFY
            script += OP_CHECKSIG                               # OP_CHECKSIG
        else:
            raise exceptions.TransactionError('Unknown encoding‐scheme.')

        s += var_int(int(len(script)))                      # Script length
        s += script

    # Change output.
    if change_output:
        change_address, change_value = change_output
        s += change_value.to_bytes(8, byteorder='little')   # Value

        if util.is_multisig(change_address):
            script = get_multisig_script(change_address)
        else:
            script = get_monosig_script(change_address)

        s += var_int(int(len(script)))                      # Script length
        s += script

    s += (0).to_bytes(4, byteorder='little')                # LockTime
    return s


class BalanceError (exceptions.TransactionError): pass
def construct (db, tx_info, encoding='auto', fee_per_kb=config.DEFAULT_FEE_PER_KB,
                 regular_dust_size=config.DEFAULT_REGULAR_DUST_SIZE,
                 multisig_dust_size=config.DEFAULT_MULTISIG_DUST_SIZE,
                 op_return_value=config.DEFAULT_OP_RETURN_VALUE,
                 exact_fee=None, fee_provided=0, self_public_key_hex=None,
                 allow_unconfirmed_inputs=False):

    block_index = util.last_block(db)['block_index']
    (source, destination_outputs, data) = tx_info


    '''Destinations'''

    # Destination outputs.
        # Replace multi‐sig addresses with multi‐sig pubkeys. Check that the
        # destination output isn’t a dust output. Set null values to dust size.
    destination_outputs_new = []
    for (address, value) in destination_outputs:

        # Value.
        if util.is_multisig(address):
            dust_size = multisig_dust_size
        else:
            dust_size = regular_dust_size

        if value == None:
            value = dust_size
        elif value < dust_size:
            raise exceptions.TransactionError('Destination output is dust.')

        # Address.
        util.validate_address(address)
        if util.is_multisig(address):
            destination_outputs_new.append((script.multisig_pubkeyhashes_to_pubkeys(address), value))
        else:
            destination_outputs_new.append((address, value))

    destination_outputs = destination_outputs_new
    destination_btc_out = sum([value for address, value in destination_outputs])


    '''Data'''

    # Data encoding methods (choose and validate).
    if data:
        if encoding == 'auto':
            if len(data) <= config.OP_RETURN_MAX_SIZE:
                # encoding = 'opreturn'
                encoding = 'multisig'   # BTCGuild isn’t mining OP_RETURN?!
            else:
                encoding = 'multisig'

        if encoding not in ('pubkeyhash', 'multisig', 'opreturn'):
            raise exceptions.TransactionError('Unknown encoding‐scheme.')

    if exact_fee and not isinstance(exact_fee, int):
        raise exceptions.TransactionError('Exact fees must be in satoshis.')
    if not isinstance(fee_provided, int):
        raise exceptions.TransactionError('Fee provided must be in satoshis.')

    # Divide data into chunks.
    if data:
        def chunks(l, n):
            """ Yield successive n‐sized chunks from l.
            """
            for i in range(0, len(l), n): yield l[i:i+n]
        if util.enabled('multisig_addresses', block_index):   # Protocol change.
            if encoding == 'pubkeyhash':
                data_array = list(chunks(data, 20 - 1 - 8)) # Prefix is also a suffix here.
            elif encoding == 'multisig':
                data_array = list(chunks(data, (33 * 2) - 1 - 8 - 2 - 2)) # Two pubkeys, minus length byte, minus prefix, minus two nonces, minus two sign bytes
        else:
            data = config.PREFIX + data
            if encoding == 'pubkeyhash':
                data_array = list(chunks(data + config.PREFIX, 20 - 1)) # Prefix is also a suffix here.
            elif encoding == 'multisig':
                data_array = list(chunks(data, 33 - 1))
        if encoding == 'opreturn':
            data_array = list(chunks(data, config.OP_RETURN_MAX_SIZE))
            assert len(data_array) == 1 # Only one OP_RETURN output currently supported (OP_RETURN messages should all be shorter than 40 bytes, at the moment).
    else:
        data_array = []

    # Data outputs.
    if encoding == 'multisig': data_value = multisig_dust_size
    elif encoding == 'opreturn': data_value = op_return_value
    else: data_value = regular_dust_size # Pay‐to‐PubKeyHash
    if data: data_output = (data_array, data_value)
    else: data_output = None
    data_btc_out = sum([data_value for data_chunk in data_array])


    '''Inputs'''

    # Source.
        # If public key is necessary for construction of (unsigned)
        # transaction, either use the public key provided, or derive it from a
        # private key retrieved from wallet.
    if source:
        util.validate_address(source)

    self_public_key = None
    if encoding in ('multisig', 'pubkeyhash'):
        if util.is_multisig(source):
            a, self_pubkeys, b = util.extract_array(script.multisig_pubkeyhashes_to_pubkeys(source))
            self_public_key = binascii.unhexlify(self_pubkeys[0])
        else:
            if not self_public_key_hex:
                # If public key was not provided, derive it from the private key.
                private_key_wif = backend.dumpprivkey(source)
                self_public_key_hex = script.private_key_to_public_key(private_key_wif)
            else:
                # If public key was provided, check that it matches the source address.
                if source != script.pubkey_to_pubkeyhash(binascii.unhexlify(self_public_key_hex)):
                    raise InputError('provided public key does not match the source address')

            # Convert hex public key into binary public key.
            try:
                self_public_key = binascii.unhexlify(self_public_key_hex)
                is_compressed = is_sec_compressed(self_public_key)
            except (EncodingError, binascii.Error):
                raise InputError('Invalid private key.')

    # Calculate collective size of outputs.
    if encoding == 'multisig': data_output_size = 81        # 71 for the data
    elif encoding == 'opreturn': data_output_size = 90      # 80 for the data
    else: data_output_size = 25 + 9                         # Pay‐to‐PubKeyHash (25 for the data?)
    outputs_size = ((25 + 9) * len(destination_outputs)) + (len(data_array) * data_output_size)

    # Get inputs.
    unspent = backend.get_unspent_txouts(source)
    unspent = backend.sort_unspent_txouts(unspent, allow_unconfirmed_inputs)
    logging.debug('Sorted UTXOs: {}'.format([print_coin(coin) for coin in unspent]))

    inputs, btc_in = [], 0
    change_quantity = 0
    sufficient_funds = False
    final_fee = fee_per_kb
    for coin in unspent:
        logging.debug('New input: {}'.format(print_coin(coin)))
        inputs.append(coin)
        btc_in += round(coin['amount'] * config.UNIT)

        # If exact fee is specified, use that. Otherwise, calculate size of tx and base fee on that (plus provide a minimum fee for selling BTC).
        if exact_fee:
            final_fee = exact_fee
        else:
            size = 181 * len(inputs) + outputs_size + 10
            necessary_fee = (int(size / 1000) + 1) * fee_per_kb
            final_fee = max(fee_provided, necessary_fee)
            assert final_fee >= 1 * fee_per_kb

        # Check if good.
        btc_out = destination_btc_out + data_btc_out
        change_quantity = btc_in - (btc_out + final_fee)
        logging.debug('Change quantity: {} BTC'.format(change_quantity / config.UNIT))
        if change_quantity == 0 or change_quantity >= regular_dust_size: # If change is necessary, must not be a dust output.
            sufficient_funds = True
            break
    if not sufficient_funds:
        # Approximate needed change, fee by with most recently calculated quantities.
        total_btc_out = btc_out + max(change_quantity, 0) + final_fee
        raise BalanceError('Insufficient bitcoins at address {}. (Need approximately {} {}.) To spend unconfirmed coins, use the flag `--unconfirmed`. (Unconfirmed coins cannot be spent from multi‐sig addresses.)'.format(source, total_btc_out / config.UNIT, config.BTC))


    '''Finish'''

    # Change output.
    if util.is_multisig(source):
        change_address = script.multisig_pubkeyhashes_to_pubkeys(source)
    else:
        change_address = source
    if change_quantity: change_output = (change_address, change_quantity)
    else: change_output = None


    # Serialise inputs and outputs.
    unsigned_tx = serialise(block_index, encoding, inputs, destination_outputs, data_output, change_output, dust_return_public_key=self_public_key)
    unsigned_tx_hex = binascii.hexlify(unsigned_tx).decode('utf-8')

    # Check that the constructed transaction isn’t doing anything funny.
    from lib import blocks
    (desired_source, desired_destination_outputs, desired_data) = tx_info
    desired_source = util.canonical_address(desired_source)
    desired_destination = util.canonical_address(desired_destination_outputs[0][0]) if desired_destination_outputs else ''
    # Include change in destinations for BTC transactions.
    if change_output and not desired_data and desired_destination != config.UNSPENDABLE:
        if desired_destination == '': desired_destination = desired_source
        else: desired_destination += '-{}'.format(desired_source)
    if desired_data == None: desired_data = b''
    parsed_source, parsed_destination, x, y, parsed_data = blocks.get_tx_info2(unsigned_tx_hex)
    if (desired_source, desired_destination, desired_data) != (parsed_source, parsed_destination, parsed_data):
        raise exceptions.TransactionError('constructed transaction does not parse correctly')

    return unsigned_tx_hex

def sign_tx (unsigned_tx_hex, private_key_wif=None):
    """Sign unsigned transaction serialisation."""

    if private_key_wif:
        for char in private_key_wif:
            if char not in util.b58_digits:
                raise exceptions.TransactionError('invalid private key')

        # TODO: Hack! (pybitcointools is Python 2 only)
        import subprocess
        i = 0
        tx_hex = unsigned_tx_hex
        while True: # pybtctool doesn’t implement `signall`
            try:
                tx_hex = subprocess.check_output(['pybtctool', 'sign', tx_hex, str(i), private_key_wif], stderr=subprocess.DEVNULL)
            except Exception as e:
                break
        if tx_hex != unsigned_tx_hex:
            signed_tx_hex = tx_hex.decode('utf-8')
            return signed_tx_hex[:-1]   # Get rid of newline.
        else:
            raise exceptions.TransactionError('Could not sign transaction with pybtctool.')

    else:   # Assume source is in wallet and wallet is unlocked.
        result = sign_raw_transaction(unsigned_tx_hex)
        if result['complete']:
            signed_tx_hex = result['hex']
        else:
            raise exceptions.TransactionError('Could not sign transaction with Bitcoin Core.')

    return signed_tx_hex

def broadcast_tx (signed_tx_hex):
    return send_raw_transaction(signed_tx_hex)

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
