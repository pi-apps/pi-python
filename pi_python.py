import logging
from typing import Optional, Dict, Any, List
from decimal import Decimal
import requests
from requests.exceptions import RequestException
import stellar_sdk as s_sdk
from dataclasses import dataclass

@dataclass
class PaymentData:
    amount: Decimal
    memo: str
    metadata: Dict[str, Any]
    user_uid: str
    identifier: str
    to_address: str
    network: Optional[str] = None
    from_address: Optional[str] = None

class PiNetworkError(Exception):
    """Base exception class for Pi Network operations"""
    pass

class PiNetwork:
    MAINNET_URL = "https://api.mainnet.minepi.com"
    TESTNET_URL = "https://api.testnet.minepi.com"
    
    def __init__(self):
        self._logger = logging.getLogger(__name__)
        self._api_key: str = ""
        self._server: Optional[s_sdk.Server] = None
        self._account: Optional[s_sdk.Account] = None
        self._base_url: str = ""
        self._network: str = ""
        self._keypair: Optional[s_sdk.Keypair] = None
        self._fee: int = 0
        self._open_payments: Dict[str, Dict] = {}

    def initialize(self, api_key: str, wallet_private_key: str, network: str) -> bool:
        try:
            if not self._validate_private_seed_format(wallet_private_key):
                raise PiNetworkError("Invalid private seed format")
                
            self._api_key = api_key
            self._load_account(wallet_private_key, network)
            self._base_url = self.MAINNET_URL if network == "Pi Network" else self.TESTNET_URL
            self._network = network
            self._fee = self._server.fetch_base_fee()
            return True
            
        except Exception as e:
            self._logger.error(f"Initialization failed: {str(e)}")
            return False

    def _load_account(self, private_seed: str, network: str):
        """Initialize Stellar SDK account"""
        self._keypair = s_sdk.Keypair.from_secret(private_seed)
        horizon_url = self.MAINNET_URL if network == "Pi Network" else self.TESTNET_URL
        self._server = s_sdk.Server(horizon_url)
        self._account = self._server.load_account(self._keypair.public_key)

    def get_balance(self) -> Decimal:
        try:
            balances = self._server.accounts().account_id(self._keypair.public_key).call()["balances"]
            for balance in balances:
                if balance["asset_type"] == "native":
                    return Decimal(balance["balance"])
            return Decimal(0)
        except Exception as e:
            self._logger.error(f"Failed to get balance: {str(e)}")
            return Decimal(0)

    def create_payment(self, payment_data: Dict[str, Any]) -> str:
        """Create a new payment"""
        try:
            if not self._validate_payment_data(payment_data):
                raise PiNetworkError("Invalid payment data")

            # Check sufficient balance
            balance = self.get_balance()
            total_cost = Decimal(payment_data["amount"]) + (Decimal(self._fee) / Decimal('10000000'))
            
            if total_cost > balance:
                return ""

            response = self._make_request(
                'POST',
                f"{self._base_url}/v2/payments",
                json={'payment': payment_data}
            )

            identifier = response.get('identifier') or response.get('payment', {}).get('identifier')
            if identifier:
                self._open_payments[identifier] = response.get('payment') or response
            return identifier or ""

        except Exception as e:
            self._logger.error(f"Payment creation failed: {str(e)}")
            return ""

    def submit_payment(self, payment_id: str, pending_payment: Optional[Dict] = None) -> str:
        """Submit a payment transaction"""
        try:
            if payment_id not in self._open_payments and not pending_payment:
                return ""
                
            payment = pending_payment if pending_payment else self._open_payments[payment_id]
            
            # Verify balance
            balance = self.get_balance()
            total_cost = Decimal(payment["amount"]) + (Decimal(self._fee) / Decimal('10000000'))
            if total_cost > balance:
                return ""

            transaction = self._build_a2u_transaction(payment)
            txid = self._submit_transaction(transaction)
            
            if payment_id in self._open_payments:
                del self._open_payments[payment_id]
                
            return txid
            
        except Exception as e:
            self._logger.error(f"Failed to submit payment: {str(e)}")
            return ""

    def _build_a2u_transaction(self, transaction_data: Dict[str, Any]) -> s_sdk.Transaction:
        """Build a Stellar transaction"""
        if not self._validate_payment_data(transaction_data):
            raise PiNetworkError("Invalid transaction data")
            
        transaction = (
            s_sdk.TransactionBuilder(
                source_account=self._account,
                network_passphrase=self._network,
                base_fee=self._fee,
            )
            .add_text_memo(transaction_data["identifier"])
            .append_payment_op(
                transaction_data["to_address"],
                s_sdk.Asset.native(),
                str(transaction_data["amount"])
            )
            .set_timeout(180)
            .build()
        )
        return transaction

    def _submit_transaction(self, transaction: s_sdk.Transaction) -> str:
        """Submit a transaction to the Stellar network"""
        transaction.sign(self._keypair)
        response = self._server.submit_transaction(transaction)
        return response["id"]

    def complete_payment(self, identifier: str, txid: Optional[str] = None) -> bool:
        """Complete a payment"""
        try:
            response = self._make_request(
                'POST',
                f"{self._base_url}/v2/payments/{identifier}/complete",
                json={'txid': txid} if txid else {}
            )
            return True
        except Exception as e:
            self._logger.error(f"Failed to complete payment: {str(e)}")
            return False

    def cancel_payment(self, identifier: str) -> bool:
        """Cancel a payment"""
        try:
            self._make_request(
                'POST',
                f"{self._base_url}/v2/payments/{identifier}/cancel",
                json={}
            )
            return True
        except Exception as e:
            self._logger.error(f"Failed to cancel payment: {str(e)}")
            return False

    def get_incomplete_server_payments(self) -> List[Dict[str, Any]]:
        """Get incomplete server payments"""
        try:
            response = self._make_request(
                'GET',
                f"{self._base_url}/v2/payments/incomplete_server_payments"
            )
            return response.get("incomplete_server_payments", [])
        except Exception as e:
            self._logger.error(f"Failed to get incomplete payments: {str(e)}")
            return []

    def _make_request(self, method: str, url: str, **kwargs) -> Dict:
        """Make HTTP request to Pi Network API"""
        try:
            headers = {
                'Authorization': f"Key {self._api_key}",
                'Content-Type': 'application/json'
            }
            kwargs['headers'] = headers
            
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
            
        except RequestException as e:
            self._logger.error(f"API request failed: {str(e)}")
            raise PiNetworkError(f"API request failed: {str(e)}")

    @staticmethod
    def _validate_private_seed_format(seed: str) -> bool:
        """Validate the format of a private seed"""
        return seed.upper().startswith('S') and len(seed) == 56

    @staticmethod
    def _validate_payment_data(data: Dict[str, Any]) -> bool:
        """Validate payment data completeness"""
        required_fields = ['amount', 'memo', 'metadata', 'user_uid', 'identifier', 'to_address']
        return all(field in data for field in required_fields)
