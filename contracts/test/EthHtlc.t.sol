// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {EthHtlc} from "../EthHtlc.sol";

contract EthHtlcTest is Test {
    EthHtlc private htlc;
    address payable private depositor = payable(address(0xA11CE));
    address payable private beneficiary = payable(address(0xB0B));
    bytes32 private constant PREIMAGE = bytes32(uint256(0x1234));
    bytes32 private hashlock;

    function setUp() public {
        htlc = new EthHtlc();
        hashlock = sha256(abi.encodePacked(PREIMAGE));
        vm.deal(depositor, 100 ether);
        vm.deal(address(this), 1 ether);
        vm.warp(1_700_000_000);
    }

    function lock(uint64 timelock, uint256 amount) private returns (bytes32 id) {
        vm.prank(depositor);
        id = htlc.lock{value: amount}(hashlock, timelock, beneficiary);
    }

    function test_lock_then_claim_pays_the_beneficiary() public {
        bytes32 id = lock(uint64(block.timestamp + 1 hours), 1 ether);
        uint256 before = beneficiary.balance;

        vm.prank(beneficiary);
        htlc.claim(id, PREIMAGE);

        assertEq(beneficiary.balance, before + 1 ether);
        EthHtlc.Lock memory stored = htlc.getLock(id);
        assertTrue(stored.claimed);
        assertEq(stored.amount, 1 ether);
    }

    function test_refund_is_only_after_the_timelock_and_only_by_the_depositor() public {
        bytes32 id = lock(uint64(block.timestamp + 1 hours), 1 ether);

        vm.expectRevert(EthHtlc.NotYetClaimable.selector);
        vm.prank(depositor);
        htlc.refund(id);

        vm.warp(block.timestamp + 1 hours);
        vm.expectRevert(EthHtlc.OnlyDepositor.selector);
        vm.prank(beneficiary);
        htlc.refund(id);

        uint256 before = depositor.balance;
        vm.prank(depositor);
        htlc.refund(id);
        assertEq(depositor.balance, before + 1 ether);
        assertTrue(htlc.getLock(id).refunded);
    }

    function test_a_wrong_preimage_is_rejected() public {
        bytes32 id = lock(uint64(block.timestamp + 1 hours), 1 ether);
        vm.expectRevert(EthHtlc.WrongPreimage.selector);
        vm.prank(beneficiary);
        htlc.claim(id, bytes32(uint256(0xBAD)));
    }

    function test_claim_after_the_timelock_is_rejected() public {
        bytes32 id = lock(uint64(block.timestamp + 1 hours), 1 ether);
        vm.warp(block.timestamp + 1 hours);
        vm.expectRevert(EthHtlc.Expired.selector);
        vm.prank(beneficiary);
        htlc.claim(id, PREIMAGE);
    }

    function test_a_closed_lock_cannot_be_claimed_or_refunded_twice() public {
        bytes32 id = lock(uint64(block.timestamp + 1 hours), 1 ether);
        vm.prank(beneficiary);
        htlc.claim(id, PREIMAGE);

        vm.expectRevert(EthHtlc.AlreadyClosed.selector);
        vm.prank(beneficiary);
        htlc.claim(id, PREIMAGE);

        vm.warp(block.timestamp + 1 hours);
        vm.expectRevert(EthHtlc.AlreadyClosed.selector);
        vm.prank(depositor);
        htlc.refund(id);
    }

    function test_lock_rejects_an_empty_value_and_a_past_timelock() public {
        vm.expectRevert(EthHtlc.EmptyValue.selector);
        vm.prank(depositor);
        htlc.lock{value: 0}(hashlock, uint64(block.timestamp + 1 hours), beneficiary);

        vm.expectRevert(EthHtlc.TimelockInThePast.selector);
        vm.prank(depositor);
        htlc.lock{value: 1 ether}(hashlock, uint64(block.timestamp), beneficiary);
    }

    function test_receive_rejects_stray_ether() public {
        vm.expectRevert(EthHtlc.NoEther.selector);
        (bool ok,) = address(htlc).call{value: 1 ether}("");
        ok;
    }
}
