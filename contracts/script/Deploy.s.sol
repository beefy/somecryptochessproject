// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {EthHtlc} from "../EthHtlc.sol";

/// @notice Deploy one EthHtlc on the chain that matches SCCP's environment.
/// @dev `SCCP_NETWORK` is `devnet`, `testnet` or `mainnet`. The expected chain id
/// is checked before broadcasting, so a devnet key cannot deploy to mainnet by
/// pointing a stale RPC URL at it.
contract Deploy is Script {
    function run() external returns (EthHtlc htlc) {
        string memory network = vm.envString("SCCP_NETWORK");
        uint256 expected = _expectedChainId(network);
        require(block.chainid == expected, "SCCP_NETWORK does not match the RPC chain id");

        vm.startBroadcast();
        htlc = new EthHtlc();
        vm.stopBroadcast();

        console2.log("SCCP network:", network);
        console2.log("Ethereum chain id:", block.chainid);
        console2.log("EthHtlc:", address(htlc));
        console2.log("Set SCCP_ETH_HTLC_CONTRACT to the address above.");
    }

    function _expectedChainId(string memory network) private pure returns (uint256) {
        bytes32 name = keccak256(bytes(network));
        if (name == keccak256("devnet")) return 31337;
        if (name == keccak256("testnet")) return 11155111;
        if (name == keccak256("mainnet")) return 1;
        revert("SCCP_NETWORK must be devnet, testnet or mainnet");
    }
}
