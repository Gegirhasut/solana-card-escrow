# Security policy

card-escrow is a reference implementation and has not been audited. The
program is upgradeable; its upgrade authority is listed in the README.

## Reporting a vulnerability

Please report privately, not in a public issue:

- GitHub: [open a private security advisory](https://github.com/Gegirhasut/solana-card-escrow/security/advisories/new)
- Email: gegirhasut@gmail.com

Include the affected instruction or endpoint, the cluster and program id, and
steps or a transaction that reproduces the problem. You will get a reply within
7 days. Please allow 90 days, or until a fix is deployed, before disclosing
publicly.

## Scope

- On-chain program `programs/card_escrow` (`8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK`)
- Backend in `backend/` (webhook authentication, authorization and settlement logic)

The mock issuer and demo tooling are out of scope.

## Verifying the deployed program

The program is built reproducibly. To check that the on-chain binary matches
this repository:

```bash
solana-verify get-program-hash -u <cluster> 8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK
solana-verify build --library-name card_escrow
solana-verify get-executable-hash target/deploy/card_escrow.so
```
