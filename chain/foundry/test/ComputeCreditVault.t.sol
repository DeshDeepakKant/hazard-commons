// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ComputeCreditVault} from "../src/ComputeCreditVault.sol";

contract ComputeCreditVaultTest is Test {
    event Deposited(string robotId, bytes32 indexed robotKey, address indexed from, uint256 amount, uint256 total);

    ComputeCreditVault vault;
    address op = makeAddr("MinuteMart");
    address other = makeAddr("ZipKart");

    function setUp() public {
        vault = new ComputeCreditVault();
        vm.deal(op, 1 ether);
        vm.deal(other, 1 ether);
    }

    function test_ownerIsDeployer() public view {
        assertEq(vault.owner(), address(this));
    }

    function test_deposit_creditsAndAccumulates() public {
        vm.prank(op);
        vault.deposit{value: 1e13}("R-04");
        assertEq(vault.creditOf("R-04"), 1e13);
        assertEq(vault.credit(keccak256("R-04")), 1e13);
        vm.prank(other);
        vault.deposit{value: 2e13}("R-04");
        assertEq(vault.creditOf("R-04"), 3e13);
        assertEq(vault.creditOf("R-05"), 0);
        assertEq(address(vault).balance, 3e13);
    }

    function test_deposit_emitsEvent() public {
        vm.prank(op);
        vault.deposit{value: 5}("R-04");
        vm.expectEmit(true, true, false, true, address(vault));
        emit Deposited("R-04", keccak256("R-04"), op, 7, 12);
        vm.prank(op);
        vault.deposit{value: 7}("R-04");
    }

    function test_deposit_zeroValueReverts() public {
        vm.prank(op);
        vm.expectRevert(ComputeCreditVault.ZeroValue.selector);
        vault.deposit{value: 0}("R-04");
    }

    function test_deposit_emptyIdReverts() public {
        vm.prank(op);
        vm.expectRevert(ComputeCreditVault.BadRobotId.selector);
        vault.deposit{value: 1}("");
    }

    function test_deposit_longIdReverts() public {
        vm.prank(op);
        vm.expectRevert(ComputeCreditVault.BadRobotId.selector);
        vault.deposit{value: 1}("012345678901234567890123456789012"); // 33 bytes
    }

    function test_deposit_32ByteIdOk() public {
        vm.prank(op);
        vault.deposit{value: 1}("01234567890123456789012345678901"); // 32 bytes
        assertEq(vault.creditOf("01234567890123456789012345678901"), 1);
    }

    function test_withdraw_onlyOwner() public {
        vm.prank(op);
        vault.deposit{value: 1e13}("R-04");
        vm.prank(op);
        vm.expectRevert(ComputeCreditVault.NotOwner.selector);
        vault.withdraw(payable(op), 1);

        address payable sink = payable(makeAddr("sink"));
        vault.withdraw(sink, 4e12);
        assertEq(sink.balance, 4e12);
        assertEq(address(vault).balance, 6e12);
        // credit is cumulative deposits, not touched by withdraw
        assertEq(vault.creditOf("R-04"), 1e13);
    }

    function test_withdraw_tooMuchReverts() public {
        vm.expectRevert(ComputeCreditVault.InsufficientBalance.selector);
        vault.withdraw(payable(op), 1);
    }
}
