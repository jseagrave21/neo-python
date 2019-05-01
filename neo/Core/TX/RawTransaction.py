import os
import binascii
import requests
import json
from neo.Core.BigInteger import BigInteger
from neo.Core.CoinReference import CoinReference
from neo.Core.Cryptography.Crypto import Crypto
from neo.Core.Fixed8 import Fixed8
from neo.Core.Helper import Helper
from neo.Core.IO.BinaryReader import BinaryReader
from neo.Core.KeyPair import KeyPair
from neo.Core.Size import GetVarSize, Size
from neo.Core.TX.Transaction import Transaction, TransactionType, TransactionOutput, TXFeeError
from neo.Core.TX.TransactionAttribute import TransactionAttribute, TransactionAttributeUsage
from neo.Core.UInt256 import UInt256
from neo.Core.UInt160 import UInt160
from neo.Core.Witness import Witness
from neo.Implementations.Wallets.peewee.UserWallet import UserWallet
from neo.IO.MemoryStream import MemoryStream
from neo.Settings import settings
from neo.SmartContract.ContractParameterContext import ContractParametersContext, Contract
from neo.Wallets.utils import to_aes_key
from neo.VM.ScriptBuilder import ScriptBuilder
from itertools import groupby


class RawTransaction(Transaction):
    """A class for building raw transactions."""

    neo_asset_id = "c56f33fc6ecfcd0c225c4ab356fee59390af8560be0e930faebe74a6daff7c9b"
    gas_asset_id = "602c79718b16e442de58778e148d0b1084e3b2dffd5de6b7b16cee7969282de7"

    _ns_mainnet = "https://neoscan.io/api/main_net"
    _ns_testnet = "https://neoscan-testnet.io/api/test_net"
    _get_balance = "/v1/get_balance/"
    _get_claimable = "/v1/get_claimable/"
    _get_transaction = "/v1/get_transaction/"

    def __init__(self, *args, **kwargs):
        """
        Create an instance.

        Args:
            *args:
            **kwargs:
        """
        super(RawTransaction, self).__init__(*args, **kwargs)
        self.raw_tx = True
        self._network = self._ns_testnet
        self._context = None
        self.__references = None
        self.SOURCE_SCRIPTHASH = None
        self.BALANCE = None

    def TXType(self, attribute):
        """
        Specify the type of transaction

        Args:
            attribute: (str) the type of transaction. Currently supports "Claim", "Contract", or "Invocation".
        """
        if attribute.lower() == "claim":
            self.Type = TransactionType.ClaimTransaction
            self.Claims = []

        elif attribute.lower() == "contract":
            self.Type = TransactionType.ContractTransaction

        elif attribute.lower() == "invocation":
            self.Script = None
            self.Gas = Fixed8(0)
            self.Type = TransactionType.InvocationTransaction

        else:
            raise TypeError("Please specify a supported transaction type.")

    def AddDescription(self, description):
        """
        Specify a description for the transaction

        Args:
            description: (str) a description attribute for the transaction
        """
        if not isinstance(description, str):
            raise TypeError('Please enter your description as a string.')

        description = description.encode('utf-8')
        if len(description) > TransactionAttribute.MAX_ATTR_DATA_SIZE:
            raise TXAttributeError(f'Maximum description length exceeded ({len(description)} > {TransactionAttribute.MAX_ATTR_DATA_SIZE})')

        if len(self.Attributes) < Transaction.MAX_TX_ATTRIBUTES:
            self.Attributes.append(TransactionAttribute(usage=TransactionAttributeUsage.Description, data=description))
        else:
            raise TXAttributeError(f'Cannot add description attribute. Maximum transaction attributes ({Transaction.MAX_TX_ATTRIBUTES}) already reached.')

    def AddDescriptionUrl(self, description_url):
        """
        Specify a description for the transaction

        Args:
            description url: (str) a description url attribute for the transaction
        """
        if not isinstance(description_url, str):
            raise TypeError('Please enter your description url as a string.')

        description_url = description_url.encode('utf-8')
        if len(description_url) > 255:
            raise TXAttributeError(f'Maximum description url length exceeded ({len(description_url)} > 255)')

        if len(self.Attributes) < Transaction.MAX_TX_ATTRIBUTES:
            self.Attributes.append(TransactionAttribute(usage=TransactionAttributeUsage.DescriptionUrl, data=description_url))
        else:
            raise TXAttributeError(f'Cannot add description url attribute. Maximum transaction attributes ({Transaction.MAX_TX_ATTRIBUTES}) already reached.')

    def AddRemark(self, remark):
        """
        Specify a remark for the transaction

        Args:
            remark: (str) a remark attribute for the transaction
        """
        if not isinstance(remark, str):
            raise TypeError('Please enter your remark as a string.')

        remark = remark.encode('utf-8')
        if len(remark) > TransactionAttribute.MAX_ATTR_DATA_SIZE:
            raise TXAttributeError(f'Maximum remark length exceeded ({len(remark)} > {TransactionAttribute.MAX_ATTR_DATA_SIZE})')

        remarks = []
        for attribute in self.Attributes:
            if attribute.Usage in range(240, 255):
                remarks.append(attribute.Usage)

        new_remark = 240
        if remarks:
            remarks = sorted(remarks)

            last_remark = remarks[-1]

            if last_remark < 255 and len(remarks) < Transaction.MAX_TX_ATTRIBUTES:
                new_remark = last_remark + 1
            else:
                raise TXAttributeError(f'Cannot add remark attribute. Maximum transaction attributes ({Transaction.MAX_TX_ATTRIBUTES}) already reached.')

        if len(self.Attributes) < Transaction.MAX_TX_ATTRIBUTES:
            self.Attributes.append(TransactionAttribute(usage=new_remark, data=remark))
        else:
            raise TXAttributeError(f'Cannot add remark attribute. Maximum transaction attributes ({Transaction.MAX_TX_ATTRIBUTES}) already reached.')

    def AddScript(self, address):
        """
        Specify a script for the transaction

        Args:
            address: (str) an additional address for transaction validation
        """
        address = Helper.AddrStrToScriptHash(address)  # also verifies if the address is valid

        if len(self.Attributes) < Transaction.MAX_TX_ATTRIBUTES:
            self.Attributes.append(TransactionAttribute(usage=TransactionAttributeUsage.Script, data=address))
        else:
            raise TXAttributeError(f'Cannot add script attribute. Maximum transaction attributes ({Transaction.MAX_TX_ATTRIBUTES}) already reached.')

    def Network(self, network):
        """
        Specify a neo-scan endpoint.

        Args:
            network: (str) the neo-scan endpoint (i.e. 'mainnet', 'testnet', or custom endpoint)
        """
        if not isinstance(network, str):
            raise TypeError('Please enter your network as a string.')

        if network.lower() == "mainnet":
            self._network = self._ns_mainnet
        elif network.lower() == "testnet":
            self._network = self._ns_testnet
        else:
            self._network = network

    def Address(self, from_addr):
        """
        Specify the originating address for the transaction.

        Args:
            from_addr: (str) the source NEO address (e.g. 'AJQ6FoaSXDFzA6wLnyZ1nFN7SGSN2oNTc3')  
        """
        src_scripthash = Helper.AddrStrToScriptHash(from_addr)  # also verifies if the address is valid
        self.SOURCE_SCRIPTHASH = src_scripthash

        url = self._network + self._get_balance + from_addr
        bal = requests.get(url=url)

        if not bal.status_code == 200:
            raise NetworkError('Neoscan request failed. Please check your internet connection.')

        bal = bal.json()
        if not bal['balance']:
            raise RawTXError(f"Address {from_addr} has a zero balance. Please ensure the correct network is selected or specify a difference source address.")
        self.BALANCE = bal['balance']

    def AddInputs(self, asset):
        """
        Specify inputs for the transaction based on the asset to be sent.
        NOTE: Can be used multiple times if sending multiple assets (i.e. NEO and GAS).

        Args:
            asset: (str) the asset name or asset hash
        """
        if not isinstance(asset, str):
            raise TypeError('Please enter the asset as a string.')

        if not self.BALANCE:
            raise RawTXError('Please specify a source address before adding inputs.')

        if asset[0:1] == "0x":
            asset == asset[2:]
        if asset.lower() == "neo":
            assetId = self.neo_asset_id
        elif asset == self.neo_asset_id:
            assetId = self.neo_asset_id
        elif asset.lower() == "gas":
            assetId = self.gas_asset_id
        elif asset == self.gas_asset_id:
            assetId = self.gas_asset_id
        else:
            raise AssetError(f'Asset {asset} not found. If trying to send tokens use the `buildTokenTransfer` function.')

        for asset in self.BALANCE:
            if assetId == asset['asset_hash']:
                if not asset['unspent']:
                    raise AssetError('No unspent assets found.')
                for unspent in asset['unspent']:
                    self.inputs.append(CoinReference(prev_hash=UInt256.ParseString(unspent['txid']), prev_index=unspent['n'])) 
        if not self.inputs:
            raise AssetError('No matching assets found at the specified source address.')

    def AddOutput(self, asset, to_addr, amount):
        """
        Specify an output for the transaction.
        NOTE: Can be used multiple times to create multiple outputs.

        Args:
            asset: (str) the asset name or asset hash
            to_addr: (str) the destination NEO address (e.g. 'AJQ6FoaSXDFzA6wLnyZ1nFN7SGSN2oNTc3')
            amount: (int/decimal) the amount of the asset to send
        """
        if asset[0:1] == "0x":
            asset == asset[2:]
        if asset.lower() == "neo":
            assetId = self.neo_asset_id
        elif asset == self.neo_asset_id:
            assetId = self.neo_asset_id
        elif asset.lower() == "gas":
            assetId = self.gas_asset_id
        elif asset == self.gas_asset_id:
            assetId = self.gas_asset_id
        else:
            raise AssetError(f'Asset {asset} not found. If trying to send tokens use the `buildTokenTransfer` function.')

        dest_scripthash = Helper.AddrStrToScriptHash(to_addr)  # also verifies if the address is valid

        if float(amount) == 0:
            raise ValueError('Amount cannot be 0.')
        f8amount = Fixed8.TryParse(amount, require_positive=True)
        if f8amount is None:
            raise ValueError('Invalid amount format.')
        elif assetId == self.neo_asset_id and (f8amount.value / Fixed8.D) != f8amount.ToInt():
            raise ValueError('Incorrect amount precision.')

        # check if the outputs exceed the available unspents
        subtotal = []
        if self.outputs:
            for output in self.outputs:
                if output.AssetId == assetId:
                    subtotal.append(output.Value.value)
        total = f8amount.value + sum(subtotal)
        total = float(Fixed8(total).ToString())
        for asset in self.BALANCE:
            if assetId == asset['asset_hash']:
                if total > asset['amount']:
                    raise AssetError('Total outputs exceed the available unspents.')

        self.outputs.append(TransactionOutput(AssetId=UInt256.ParseString(assetId), Value=f8amount, script_hash=dest_scripthash))

    def AddNetworkFee(self, fee):
        """
        Specify a priority network fee.

        Args:
            fee: (decimal) the priority network fee
        """
        if self.Type == b'\x02':  # ClaimTransaction
            raise RawTXError('Network fees are not required for ClaimTransactions.')

        fee = Fixed8.FromDecimal(fee)
        self._network_fee = fee

    def CalcChange(self, change_addr=None):
        """
        Calculates the change output(s). NOTE: Assumes all other outputs have been added.

        Args:
            change_addr: (str, optional) specify a change address. NOTE: Defaults to the sourceAddress.
        """
        if not change_addr:
            change_addr = self.SOURCE_SCRIPTHASH
        if change_addr != self.SOURCE_SCRIPTHASH:
            change_hash = Helper.AddrStrToScriptHash(change_addr)  # also verifies if the address is valid
        else:
            change_hash = change_addr

        if not self.outputs:
            raise RawTXError("Please specify outputs prior to creating change output(s).")

        neo = []
        gas = []
        for output in self.outputs:
            if output.AssetId == UInt256.ParseString(self.neo_asset_id):
                neo.append(output.Value.value)
            elif output.AssetId == UInt256.ParseString(self.gas_asset_id):
                gas.append(output.Value.value)
        if self.SystemFee() > Fixed8.Zero():
            gas.append(self.SystemFee().value)
        if self._network_fee:
            if self._network_fee > Fixed8.Zero():
                gas.append(self._network_fee.value)
        neo_total = 0
        gas_total = 0
        for asset in self.BALANCE:
            if asset['asset_hash'] == self.neo_asset_id:
                neo_total = asset['amount']
            elif asset['asset_hash'] == self.gas_asset_id:
                gas_total = asset['amount']
        neo_diff = Fixed8.FromDecimal(neo_total) - Fixed8(sum(neo))
        gas_diff = Fixed8.FromDecimal(gas_total) - Fixed8(sum(gas))

        if neo_diff < Fixed8.Zero() or gas_diff < Fixed8.Zero():
            raise AssetError('Total outputs exceed the available unspents.')

        if neo_diff > Fixed8.Zero():
            self.outputs.append(TransactionOutput(AssetId=UInt256.ParseString(self.neo_asset_id), Value=neo_diff, script_hash=change_hash))
        if gas_diff > Fixed8.Zero() and Fixed8(sum(gas)) > Fixed8.Zero():
            self.outputs.append(TransactionOutput(AssetId=UInt256.ParseString(self.gas_asset_id), Value=gas_diff, script_hash=change_hash))

    def AddClaim(self, claim_addr, to_addr=None):
        """
        Builds a claim transaction for the specified address.

        Args:
            claim_addr: (str) the address from which the claim is being constructed (e.g. 'AJQ6FoaSXDFzA6wLnyZ1nFN7SGSN2oNTc3'). NOTE: Claimed GAS is sent to the claim_addr by default
            to_addr: (str, optional) specify a different destination NEO address (e.g. 'AJQ6FoaSXDFzA6wLnyZ1nFN7SGSN2oNTc3')
        """
        dest_scripthash = Helper.AddrStrToScriptHash(claim_addr)  # also verifies if the address is valid
        self.SOURCE_SCRIPTHASH = dest_scripthash

        url = self._network + self._get_claimable + claim_addr
        res = requests.get(url=url)

        if not res.status_code == 200:
            raise NetworkError('Neoscan request failed. Please check your internet connection.')

        res = res.json()
        available = res["unclaimed"]
        if available == 0:
            raise AssetError(f"Address {claim_addr} has 0 unclaimed GAS. Please ensure the correct network is selected or specify a difference source address.")

        for ref in res['claimable']:
            self.Claims.append(CoinReference(prev_hash=UInt256.ParseString(ref['txid']), prev_index=ref['n']))

        if to_addr:
            dest_scripthash = Helper.AddrStrToScriptHash(to_addr)  # also verifies if the address is valid

        self.outputs.append(TransactionOutput(AssetId=UInt256.ParseString(self.gas_asset_id), Value=Fixed8.FromDecimal(available), script_hash=dest_scripthash))

    def BuildTokenTransfer(self, token, to_addr, amount):
        """
        Build a token transfer for an InvocationTransaction.

        Args:
            token: (str) the asset symbol or asset hash
            to_addr: (str) the destination NEO address (e.g. 'AJQ6FoaSXDFzA6wLnyZ1nFN7SGSN2oNTc3')
            amount: (int/decimal) the amount of the asset to send
        """
        if not self.BALANCE:
            raise RawTXError('Please specify a source address before building a token transfer.')

        if not isinstance(token, str):
            raise TypeError('Please enter your token as a string.')

        # check if the token is in the source addr balance
        t_hash = None
        if len(token) == 40:  # check if token is a scripthash
            for asset in self.BALANCE:
                if asset['asset_hash'] == token:
                    t_hash = asset['asset_hash']

                    # also verify not insufficient funds
                    if asset['amount'] < amount:
                        raise AssetError("Insufficient funds.")
                    break
        else:  # assume the symbol was used
            for asset in self.BALANCE:
                if asset['asset_symbol'] == token:
                    t_hash = asset['asset_hash']

                    # also verify not insufficient funds
                    if asset['amount'] < amount:
                        raise AssetError("Insufficient funds.")   
                    break 
        if not t_hash:
            raise AssetError(f'Token {token} not found in the source address balance.')

        dest_scripthash = Helper.AddrStrToScriptHash(to_addr)  # also verifies if the address is valid

        sb = ScriptBuilder()
        sb.EmitAppCallWithOperationAndArgs(UInt160.ParseString(t_hash), 'transfer', [self.SOURCE_SCRIPTHASH.Data, dest_scripthash.Data, BigInteger(Fixed8.FromDecimal(amount).value)])
        script = sb.ToArray()

        self.Version = 1
        self.Script = binascii.unhexlify(script)

        # check to see if the source address has been added as a script attribute and add it if not found
        s = 0
        for attr in self.Attributes:
            if attr.Usage == TransactionAttributeUsage.Script:
                s = s + 1
        if s == 0:
            self.Attributes.append(TransactionAttribute(usage=TransactionAttributeUsage.Script, data=self.SOURCE_SCRIPTHASH))
        if s > 1:
            raise TXAttributeError('The script attribute must be used to verify the source address.')

    def ImportFromArray(self, raw_tx):
        """
        Import a raw transaction from an array.

        Args:
            raw_tx: (bytes) the raw transaction array
        """
        if not isinstance(raw_tx, bytes):
            raise TypeError('Please input a byte array.')

        try:
            raw_tx = binascii.unhexlify(raw_tx)
            ms = MemoryStream(raw_tx)
            reader = BinaryReader(ms)
            self.DeserializeFrom(reader)
        except Exception as e:
            raise FormatError(f'Unable to import raw transaction.\nError output: {e}')

    def Sign(self, NEP2orWIF, NEP2password=None, multisig_args=[]):
        """
        Sign the raw transaction

        Args:
            NEP2orWIF: (str) the NEP2 or WIF key string from the address you are sending from. NOTE: Assumes WIF if NEP2password is None.
            NEP2password: (str, optional) the NEP2 password associated with the NEP2 key string. Defaults to None.
            multisig_args: (list, optional) the arguments for importing a multsig address (e.g. [<owner pubkey>, <num required sigs>, [<signing pubkey>, ...]])
        """
        temp_path = "temp_wallet.wallet"
        temp_password = "1234567890"
        wallet = UserWallet.Create(temp_path, to_aes_key(temp_password), generate_default_key=False)
        if NEP2password:
            private_key = KeyPair.PrivateKeyFromNEP2(NEP2orWIF, NEP2password)
        else:
            private_key = KeyPair.PrivateKeyFromWIF(NEP2orWIF)
        wallet.CreateKey(private_key)

        if multisig_args:  # import a multisig address
            verification_contract = Contract.CreateMultiSigContract(Crypto.ToScriptHash(multisig_args[0], unhex=True), multisig_args[1], multisig_args[2])
            wallet.AddContract(verification_contract)

        if self.Type == b'\xd1' and not self.SOURCE_SCRIPTHASH:  # in case of an invocation with no funds transfer
            context = ContractParametersContext(self)
        elif not self._context:  # used during transactions involving a funds transfer
            signer_contract = wallet.GetContract(self.SOURCE_SCRIPTHASH)
            context = ContractParametersContext(self, isMultiSig=signer_contract.IsMultiSigContract)
        else:
            context = self._context  # used for a follow-on signature for a multi-sig transaction

        wallet.Sign(context)
        if context.Completed:
            self.scripts = context.GetScripts()
            self.Validate()  # ensure the tx is ready to be relayed
        elif context.ContextItems:
            self._context = context
            print("Transaction initiated, but the signature is incomplete. Sign again with another valid multi-sig keypair.")
        else:
            raise SignatureError("Unable to sign transaction.")
        wallet.Close()
        wallet = None
        os.remove(temp_path)

    def GetTXID(self):
        """
        Returns the hash of the transaction.
        """
        return self.Hash.ToString()

    def GetRawTX(self):
        """
        Returns the transaction array, which is the input for "params" if sending via "sendrawtransaction".
        """
        return self.ToArray()

    def ToJson(self):
        """
        Convert object members to a dictionary that can be parsed as JSON.

        Returns:
             dict:
        """
        if self.Type == b'\x02':  # ClaimTransaction
            json = super(RawTransaction, self).ToJson()
            json['claims'] = [claim.ToJson() for claim in self.Claims]
            return json
        elif self.Type == b'\xd1':  # InvocationTransaction
            jsn = super(RawTransaction, self).ToJson()
            jsn['script'] = self.Script.hex()
            jsn['gas'] = self.Gas.ToNeoJsonString()
            return jsn
        else:
            return super(RawTransaction, self).ToJson()

    def Validate(self):
        """
        Validate the transaction.
        """
        if self.Size() > self.MAX_TX_SIZE:
            raise TXAttributeError('Maximum transaction size exceeded.')

        # calculate and verify the required network fee for the tx
        if not self._network_fee:
            self._network_fee = Fixed8.Zero()
        fee = self._network_fee
        if self.Size() > settings.MAX_FREE_TX_SIZE and not self.Type == b'\x02':  # Claim Transactions are High Priority
            req_fee = Fixed8.FromDecimal(settings.FEE_PER_EXTRA_BYTE * (self.Size() - settings.MAX_FREE_TX_SIZE))
            if req_fee < settings.LOW_PRIORITY_THRESHOLD:
                req_fee = settings.LOW_PRIORITY_THRESHOLD
            if fee < req_fee:
                raise TXFeeError(f'The tx size ({self.Size()}) exceeds the max free tx size ({settings.MAX_FREE_TX_SIZE}).\nA network fee of {req_fee.ToString()} GAS is required.')

    @property
    def References(self):
        """
        Get all references.

        Returns:
            dict:
                Key (UInt256): input PrevHash
                Value (TransactionOutput): object.
        """
        if self.__references is None:
            refs = {}
            # group by the input prevhash
            for hash, group in groupby(self.inputs, lambda x: x.PrevHash):
                url = self._network + self._get_transaction + hash.ToString()
                tx = requests.get(url=url)

                if not tx.status_code == 200:
                    raise NetworkError('Neoscan request failed. Please check your internet connection.')
                tx = tx.json()

                if tx is not None:
                    for input in group:
                        t = tx['vouts'][input.PrevIndex]
                        if t['asset'].lower() == 'neo':
                            asset = UInt256.ParseString(self.neo_asset_id)
                        elif t['asset'].lower() == 'gas':
                            asset = UInt256.ParseString(self.gas_asset_id)
                        refs[input] = TransactionOutput(AssetId=asset, Value=Fixed8.FromDecimal(t['value']), script_hash=Helper.AddrStrToScriptHash(t['address_hash']))

            self.__references = refs

        return self.__references

    def Size(self):
        """
        Get the total size in bytes of the object.

        Returns:
            int: size.
        """
        if self.Type == b'\x02':  # ClaimTransaction
            return super(RawTransaction, self).Size() + GetVarSize(self.Claims)
        elif self.Type == b'\xd1':  # InvocationTransaction
            sizeGas = Size.uint64 if self.Version >= 1 else 0
            return super(RawTransaction, self).Size() + GetVarSize(self.Script) + sizeGas
        else:
            return super(RawTransaction, self).Size()

    def SystemFee(self):
        """
        Get the system fee.

        Returns:
            Fixed8:
        """
        if self.Type == b'\xd1':  # InvocationTransaction
            return self.Gas
        else:
            return super(RawTransaction, self).SystemFee()

    def NetworkFee(self):
        """
        Get the network fee.

        Returns:
            Fixed8:
        """
        if self.Type == b'\x02':  # ClaimTransaction
            return Fixed8(0)
        else:
            return super(RawTransaction, self).NetworkFee()

    def SerializeExclusiveData(self, writer):
        """
        Serialize object.

        Args:
            writer (neo.IO.BinaryWriter):
        """
        if self.Type == b'\x02':  # ClaimTransaction
            writer.WriteSerializableArray(self.Claims)

        elif self.Type == b'\xd1':  # InvocationTransaction
            writer.WriteVarBytes(self.Script)
            if self.Version >= 1:
                writer.WriteFixed8(self.Gas)
        else:
            super(RawTransaction, self).SerializeExclusiveData(writer=writer)

    def DeserializeExclusiveData(self, reader):
        """
        Deserialize full object.

        Args:
            reader (neo.IO.BinaryReader):
        """
        if self.Type == b'\x02':  # ClaimTransaction
            if self.Version != 0:
                raise FormatError('Invalid format')

            numrefs = reader.ReadVarInt()

            claims = []
            for i in range(0, numrefs):
                c = CoinReference()
                c.Deserialize(reader)
                claims.append(c)

            self.Claims = claims
            if len(self.Claims) == 0:
                raise FormatError('Invalid format')

        elif self.Type == b'\xd1':  # InvocationTransaction
            if self.Version > 1:
                raise FormatError('Invalid format')

            self.Script = reader.ReadVarBytes()

            if len(self.Script) == 0:
                raise FormatError('Invalid Format')

            if self.Version >= 1:
                self.Gas = reader.ReadFixed8()
                if self.Gas < Fixed8.Zero():
                    raise FormatError("Invalid Format")
            else:
                self.Gas = Fixed8(0)
        else:
            super(RawTransaction, self).DeserializeExclusiveData(reader=reader)

    def GetScriptHashesForVerifying(self):
        """
        Get a list of script hashes for verifying transactions.

        Returns:
            list: of UInt160 type script hashes.
        """
        if self.Type == b'\x02':  # ClaimTransaction 
            hashes = super(RawTransaction, self).GetScriptHashesForVerifying()

            for hash, group in groupby(self.Claims, lambda x: x.PrevHash):
                url = self._network + self._get_transaction + hash.ToString()
                tx = requests.get(url=url)

                if not tx.status_code == 200:
                    raise NetworkError('Neoscan request failed. Please check your internet connection.')
                tx = tx.json()

                if tx is None:
                    raise RawTXError("Invalid Claim Operation")

                for claim in group:
                    if len(tx['vouts']) <= claim.PrevIndex:
                        raise RawTXError("Invalid Claim Operation")

                    script_hash = Helper.AddrStrToScriptHash(tx['vouts'][claim.PrevIndex]['address_hash'])

                    if script_hash not in hashes:
                        hashes.append(script_hash)

            hashes.sort()

            return hashes
        else:
            return super(RawTransaction, self).GetScriptHashesForVerifying()

    def DeserializeFrom(self, reader):
        """
        Deserialize full object.

        Args:
            reader (neo.IO.BinaryReader):
        """
        ttype = reader.ReadByte()

        if ttype == int.from_bytes(TransactionType.RegisterTransaction, 'little'):
            self.Type = TransactionType.RegisterTransaction
        elif ttype == int.from_bytes(TransactionType.MinerTransaction, 'little'):
            self.Type = TransactionType.MinerTransaction
        elif ttype == int.from_bytes(TransactionType.IssueTransaction, 'little'):
            self.Type = TransactionType.IssueTransaction
        elif ttype == int.from_bytes(TransactionType.ClaimTransaction, 'little'):
            self.Type = TransactionType.ClaimTransaction
        elif ttype == int.from_bytes(TransactionType.PublishTransaction, 'little'):
            self.Type = TransactionType.PublishTransaction
        elif ttype == int.from_bytes(TransactionType.InvocationTransaction, 'little'):
            self.Type = TransactionType.InvocationTransaction
        elif ttype == int.from_bytes(TransactionType.EnrollmentTransaction, 'little'):
            self.Type = TransactionType.EnrollmentTransaction
        elif ttype == int.from_bytes(TransactionType.StateTransaction, 'little'):
            self.Type = TransactionType.StateTransaction
        else:
            self.Type = ttype

        self.DeserializeUnsignedWithoutType(reader)

        self.scripts = []
        byt = reader.ReadVarInt()

        if byt > 0:
            for i in range(0, byt):
                witness = Witness()
                witness.Deserialize(reader)

                self.scripts.append(witness)

        self.OnDeserialized()


class RawTXError(Exception):
    """Provide user-friendly feedback for RawTransaction errors"""
    pass


class NetworkError(Exception):
    """Provide user-friendly feedback for network errors"""
    pass


class TXAttributeError(Exception):
    """Provide user-friendly feedback for transaction attribute errors"""
    pass


class AssetError(Exception):
    """Provide user-friendly feedback for asset errors"""
    pass


class SignatureError(Exception):
    """Provide user-friendly feedback for signature errors"""
    pass


class FormatError(Exception):
    """Provide user-friendly feedback for format errors"""
    pass
