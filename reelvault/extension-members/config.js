// The ReelVault servers this build may talk to (DESIGN-SAAS.md 5.2). The extension refuses every other server.
// deploy/build_extension.py and the server's ZIP download write the two lines below. Do not edit them by hand.
export const SERVERS = ["https://app.example.com"];
export const BUILD = "zip";
