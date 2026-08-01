"""Solana execution lane — venue clients for the supervised SOL->USDC swap.

PR-S1 is the CLIENT layer only: quoting, transaction inspection, local
signing, read-only RPC, Jito submission and submission resolution. The
supervised runner that composes them (envelope, approval boundary, evidence)
is PR-S2 and does not live here.

Submission topology, which is the lane's central safety property::

    Jupiter  --build-->  tx_inspector --verify--> signer --sign--> Jito
       ^                                                            |
       |                                                            v
    RPC (READ + SIMULATE ONLY, never broadcast) <---- resolver <----+

``rpc_client`` implements no ``send_transaction`` method at all. The absence
is the guarantee: there is no public-RPC broadcast path to disable, misuse or
re-enable by accident.
"""

from __future__ import annotations
