# Host readiness checklist

- [ ] Dedicated, supported Windows or Linux host with reliable storage, current security updates and restricted administrator access.
- [ ] Python 3.14, PostgreSQL, `pg_dump`/`pg_restore`, `age` encryption and a supervised service manager installed from approved sources.
- [ ] Application and database run as restricted service accounts; database accepts only local/application-server connections.
- [ ] Static LAN address or reserved DHCP address; internal DNS name and trusted HTTPS certificate configured.
- [ ] Firewall permits only workstation HTTPS to the application. PostgreSQL is not exposed to workstations or the internet.
- [ ] Reception, clinical and pharmacy computers are repaired/verified; supported browsers and printers are tested.
- [ ] Reliable router/switch and cabling; workstation access verified during internet loss.
- [ ] UPS and surge protection sized for safe shutdown, with tested automatic/manual shutdown procedure.
- [ ] Separate encrypted backup media is present; owner holds the recovery key; daily schedule and failure alert are active.
- [ ] Restore rehearsal completed on another environment and signed off with ledger/balance comparisons.
- [ ] Desktop shortcut created on each authorised workstation with `scripts/create-desktop-shortcut.ps1` using the real internal HTTPS URL.
- [ ] Numbered downtime forms printed and controlled; staff rehearse server-loss and later back-entry.
- [ ] Private owner remote-access path, additional authentication and freshness display tested, or explicitly left disabled.

Broken or unspecified hardware is not treated as operational merely because the application runs on another computer.
