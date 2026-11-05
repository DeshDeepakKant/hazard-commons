// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {HazardRegistry} from "../src/HazardRegistry.sol";

contract Rejector {
    // an "operator" contract that refuses ETH: payouts must be deferred, not revert the vote
    receive() external payable {
        revert("no");
    }

    function call(address target, bytes calldata data) external payable returns (bytes memory) {
        (bool ok, bytes memory ret) = target.call{value: msg.value}(data);
        require(ok, "call failed");
        return ret;
    }
}

contract HazardRegistryTest is Test {
    HazardRegistry reg;
    address zip = makeAddr("ZipKart");
    address minute = makeAddr("MinuteMart");
    address dash = makeAddr("DashKirana");
    uint256 constant STAKE = 1e14;

    bytes32 constant R_ZIP = keccak256("R-01");
    bytes32 constant D_ZIP = keccak256("D-01");
    bytes32 constant R_MIN = keccak256("R-04");
    bytes32 constant R_DASH = keccak256("R-07");
    bytes32 constant EDGE = keccak256("e42");
    bytes32 constant ROOT = bytes32(uint256(0xabc));

    function setUp() public {
        reg = new HazardRegistry(STAKE, 1, 1, 1e13);
        reg.registerOperator(zip, "ZipKart");
        reg.registerOperator(minute, "MinuteMart");
        reg.registerOperator(dash, "DashKirana");
        _robots(zip, R_ZIP, 0);
        _robots(zip, D_ZIP, 1);
        _robots(minute, R_MIN, 0);
        _robots(dash, R_DASH, 0);
        vm.deal(zip, 1 ether);
        vm.deal(minute, 1 ether);
        vm.deal(dash, 1 ether);
    }

    function _robots(address op, bytes32 id, uint8 kind) internal {
        bytes32[] memory ids = new bytes32[](1);
        uint8[] memory kinds = new uint8[](1);
        ids[0] = id;
        kinds[0] = kind;
        vm.prank(op);
        reg.registerRobots(ids, kinds);
    }

    function _report(address op, bytes32 robot, uint8 t) internal returns (uint256) {
        vm.prank(op);
        return reg.reportHazard{value: STAKE}(robot, EDGE, "e42", t, 80, 2, ROOT, "req-1");
    }

    function test_confirmPath() public {
        reg.fundRewards{value: 1e15}();
        uint256 id = _report(minute, R_MIN, 1);
        assertEq(id, 1);
        assertEq(address(reg).balance, 1e15 + STAKE);
        uint256 before = minute.balance;
        vm.prank(zip);
        reg.verifyHazard(id, D_ZIP, true, ROOT, "req-2");
        HazardRegistry.Hazard memory h = reg.getHazard(id);
        assertEq(uint8(h.status), uint8(HazardRegistry.Status.Confirmed));
        assertEq(h.confirms, 1);
        assertEq(h.stake, 0);
        assertEq(minute.balance, before + STAKE + 1e13); // refund + reward
        assertEq(reg.rewardPool(), 1e15 - 1e13);
    }

    function test_confirmWithEmptyPoolRefundsStakeOnly() public {
        uint256 id = _report(minute, R_MIN, 1);
        uint256 before = minute.balance;
        vm.prank(dash);
        reg.verifyHazard(id, R_DASH, true, ROOT, "req-2");
        assertEq(minute.balance, before + STAKE);
    }

    function test_disputeSlashPath() public {
        uint256 id = _report(dash, R_DASH, 2);
        uint256 before = zip.balance;
        vm.prank(zip);
        reg.verifyHazard(id, D_ZIP, false, ROOT, "req-3");
        HazardRegistry.Hazard memory h = reg.getHazard(id);
        assertEq(uint8(h.status), uint8(HazardRegistry.Status.Rejected));
        assertEq(h.disputes, 1);
        assertEq(zip.balance, before + STAKE);
        assertEq(address(reg).balance, 0);
    }

    function test_disputeSplitAcrossTwoDisputers() public {
        reg.setConfig(STAKE, 3, 2, 0);
        uint256 id = _report(dash, R_DASH, 2);
        uint256 z0 = zip.balance;
        uint256 m0 = minute.balance;
        vm.prank(zip);
        reg.verifyHazard(id, D_ZIP, false, ROOT, "a");
        assertEq(uint8(reg.getHazard(id).status), uint8(HazardRegistry.Status.Reported));
        vm.prank(minute);
        reg.verifyHazard(id, R_MIN, false, ROOT, "b");
        assertEq(uint8(reg.getHazard(id).status), uint8(HazardRegistry.Status.Rejected));
        assertEq(zip.balance - z0 + minute.balance - m0, STAKE);
    }

    function test_sameOperatorVerifyReverts() public {
        uint256 id = _report(zip, R_ZIP, 1);
        vm.prank(zip);
        vm.expectRevert(HazardRegistry.SameOperator.selector);
        reg.verifyHazard(id, D_ZIP, true, ROOT, "x");
    }

    function test_otherOperatorsRobotReverts() public {
        uint256 id = _report(zip, R_ZIP, 1);
        vm.prank(minute);
        vm.expectRevert(HazardRegistry.NotYourRobot.selector);
        reg.verifyHazard(id, R_DASH, true, ROOT, "x");
    }

    function test_doubleVoteReverts() public {
        reg.setConfig(STAKE, 2, 2, 0);
        uint256 id = _report(zip, R_ZIP, 1);
        vm.prank(minute);
        reg.verifyHazard(id, R_MIN, true, ROOT, "x");
        vm.prank(minute);
        vm.expectRevert(HazardRegistry.AlreadyVoted.selector);
        reg.verifyHazard(id, R_MIN, false, ROOT, "y");
    }

    function test_voteAfterResolutionReverts() public {
        uint256 id = _report(zip, R_ZIP, 1);
        vm.prank(minute);
        reg.verifyHazard(id, R_MIN, true, ROOT, "x");
        vm.prank(dash);
        vm.expectRevert(HazardRegistry.WrongStatus.selector);
        reg.verifyHazard(id, R_DASH, false, ROOT, "y");
    }

    function test_stakeTooLowReverts() public {
        vm.prank(zip);
        vm.expectRevert(HazardRegistry.StakeTooLow.selector);
        reg.reportHazard{value: STAKE - 1}(R_ZIP, EDGE, "e42", 1, 50, 0, ROOT, "r");
    }

    function test_badInputReverts() public {
        vm.startPrank(zip);
        vm.expectRevert(HazardRegistry.BadInput.selector);
        reg.reportHazard{value: STAKE}(R_ZIP, EDGE, "e42", 0, 50, 0, ROOT, "r");
        vm.expectRevert(HazardRegistry.BadInput.selector);
        reg.reportHazard{value: STAKE}(R_ZIP, EDGE, "e42", 1, 101, 0, ROOT, "r");
        vm.stopPrank();
    }

    function test_nonOperatorReverts() public {
        address rando = makeAddr("rando");
        vm.deal(rando, 1 ether);
        vm.prank(rando);
        vm.expectRevert(HazardRegistry.NotOperator.selector);
        reg.reportHazard{value: STAKE}(R_ZIP, EDGE, "e42", 1, 50, 0, ROOT, "r");
    }

    function test_robotTakenReverts() public {
        bytes32[] memory ids = new bytes32[](1);
        uint8[] memory kinds = new uint8[](1);
        ids[0] = R_ZIP;
        vm.prank(dash);
        vm.expectRevert(HazardRegistry.RobotTaken.selector);
        reg.registerRobots(ids, kinds);
    }

    function test_onlyOwnerAdmin() public {
        vm.prank(zip);
        vm.expectRevert(HazardRegistry.NotOwner.selector);
        reg.registerOperator(zip, "Evil");
        vm.prank(zip);
        vm.expectRevert(HazardRegistry.NotOwner.selector);
        reg.setConfig(0, 1, 1, 0);
    }

    function test_clear() public {
        uint256 id = _report(minute, R_MIN, 1);
        // cannot clear an unconfirmed report
        vm.prank(dash);
        vm.expectRevert(HazardRegistry.WrongStatus.selector);
        reg.clearHazard(id, ROOT, "c");
        vm.prank(zip);
        reg.verifyHazard(id, D_ZIP, true, ROOT, "v");
        vm.prank(dash);
        reg.clearHazard(id, ROOT, "c");
        assertEq(uint8(reg.getHazard(id).status), uint8(HazardRegistry.Status.Cleared));
        vm.prank(dash);
        vm.expectRevert(HazardRegistry.WrongStatus.selector);
        reg.clearHazard(id, ROOT, "c");
    }

    function test_recordDelivery() public {
        vm.prank(zip);
        reg.recordDelivery(keccak256("O-1"), R_ZIP, ROOT);
        vm.prank(zip);
        vm.expectRevert(HazardRegistry.NotYourRobot.selector);
        reg.recordDelivery(keccak256("O-1"), R_MIN, ROOT);
    }

    function test_rejectingDisputerGetsCredit() public {
        Rejector rj = new Rejector();
        reg.registerOperator(address(rj), "Rejector");
        bytes32 rid = keccak256("R-99");
        bytes32[] memory ids = new bytes32[](1);
        uint8[] memory kinds = new uint8[](1);
        ids[0] = rid;
        rj.call(address(reg), abi.encodeCall(HazardRegistry.registerRobots, (ids, kinds)));
        uint256 id = _report(dash, R_DASH, 2);
        rj.call(address(reg), abi.encodeCall(HazardRegistry.verifyHazard, (id, rid, false, ROOT, "z")));
        assertEq(uint8(reg.getHazard(id).status), uint8(HazardRegistry.Status.Rejected));
        assertEq(reg.credits(address(rj)), STAKE);
    }
}
