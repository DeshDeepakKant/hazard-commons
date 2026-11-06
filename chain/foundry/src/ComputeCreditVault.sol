// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ComputeCreditVault: per-robot compute-credit deposits on 0G Galileo (TESTNET ONLY).
/// @notice Robot operators pay testnet 0G into a per-robot compute-credit account with a real tx.
///         The vault only holds funds and emits `Deposited`; the off-chain mock testnet Router
///         stand-in indexes `Deposited` events and credits the robot's compute balance from them.
///         robotKey = keccak256(bytes(robotId)). The owner can withdraw to recover testnet funds.
/// @dev Testnet only. No accounting of spend on chain: `credit` is the cumulative amount deposited.
contract ComputeCreditVault {
    /// @notice Emitted on every deposit. `total` is the robot's cumulative credit after this deposit.
    event Deposited(string robotId, bytes32 indexed robotKey, address indexed from, uint256 amount, uint256 total);
    event Withdrawn(address indexed to, uint256 amount);

    error NotOwner();
    error ZeroValue();
    error BadRobotId();
    error InsufficientBalance();
    error TransferFailed();

    address public immutable owner;

    /// @notice robotKey => cumulative wei deposited for that robot.
    mapping(bytes32 => uint256) public credit;

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    /// @notice Deposit msg.value as compute credit for `robotId` (1..32 bytes, e.g. "R-04").
    function deposit(string calldata robotId) external payable {
        if (msg.value == 0) revert ZeroValue();
        uint256 n = bytes(robotId).length;
        if (n == 0 || n > 32) revert BadRobotId();
        bytes32 key = keccak256(bytes(robotId));
        uint256 total = credit[key] + msg.value;
        credit[key] = total;
        emit Deposited(robotId, key, msg.sender, msg.value, total);
    }

    /// @notice Cumulative wei deposited for `robotId`.
    function creditOf(string calldata robotId) external view returns (uint256) {
        return credit[keccak256(bytes(robotId))];
    }

    /// @notice Owner-only: recover testnet funds.
    function withdraw(address payable to, uint256 amount) external onlyOwner {
        if (amount > address(this).balance) revert InsufficientBalance();
        (bool ok,) = to.call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit Withdrawn(to, amount);
    }
}
