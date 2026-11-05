// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title HazardRegistry: a shared, staked hazard map for rival delivery-robot operators.
/// @notice Operators (one signer address each) register their robots, report road hazards with a tiny
///         stake, and verify each other's reports. Robots of a DIFFERENT operator than the reporter
///         vote confirm/dispute (one vote per operator per hazard). Enough confirms -> Confirmed (stake
///         refunded + reward from the pool). Enough disputes -> Rejected (stake slashed to disputers).
///         Every state change is an event carrying enough data to rebuild the whole map from logs.
contract HazardRegistry {
    // ------------------------------------------------------------------ types
    enum Status {
        None,
        Reported,
        Confirmed,
        Rejected,
        Cleared
    }

    struct Hazard {
        address reporter; // operator signer
        uint8 hazardType; // 1=accident, 2=road_works, 3=waterlogging
        uint8 severity; // 0..100
        uint8 passableMask; // bit0 rover, bit1 drone
        Status status;
        uint8 confirms;
        uint8 disputes;
        uint64 reportedAt;
        uint128 stake;
        bytes32 robotId;
        bytes32 edgeId;
        bytes32 evidenceRoot;
    }

    struct Robot {
        address operator;
        uint8 kind; // 0=rover, 1=drone
    }

    // ------------------------------------------------------------------ config
    address public owner;
    uint256 public minStake;
    uint8 public confirmsNeeded;
    uint8 public disputesNeeded;
    uint256 public rewardPerConfirm; // paid from rewardPool to the reporter on Confirmed
    uint256 public rewardPool;

    // ------------------------------------------------------------------ state
    mapping(address => string) public operatorName; // empty = not an operator
    address[] public operatorList;
    mapping(bytes32 => Robot) public robots;
    Hazard[] internal _hazards; // hazardId = index + 1
    mapping(uint256 => mapping(address => bool)) public hasVoted;
    mapping(uint256 => address[]) internal _disputers;
    mapping(address => uint256) public credits; // payouts whose push transfer failed
    uint256 private _lock = 1;

    // ------------------------------------------------------------------ events
    event OperatorRegistered(address indexed operator, string name);
    event RobotsRegistered(address indexed operator, bytes32[] robotIds, uint8[] kinds);
    event HazardReported(
        uint256 indexed hazardId,
        bytes32 indexed edgeId,
        address indexed operator,
        bytes32 robotId,
        string edgeName,
        uint8 hazardType,
        uint8 severity,
        uint8 passableMask,
        uint256 stake,
        bytes32 evidenceRoot,
        string zgRequestId
    );
    event HazardVerified(
        uint256 indexed hazardId,
        address indexed operator,
        bytes32 indexed robotId,
        bool confirm,
        uint8 confirms,
        uint8 disputes,
        bytes32 evidenceRoot,
        string zgRequestId
    );
    /// status: 2=Confirmed (payout = stake refund + reward to reporter), 3=Rejected (payout = slashed stake split
    /// across disputers).
    event HazardResolved(uint256 indexed hazardId, uint8 status, address indexed reporter, uint256 payout);
    event HazardCleared(
        uint256 indexed hazardId, address indexed operator, bytes32 evidenceRoot, string zgRequestId
    );
    event DeliveryRecorded(
        bytes32 indexed orderId, address indexed operator, bytes32 indexed robotId, bytes32 episodeRoot
    );
    event ConfigUpdated(uint256 minStake, uint8 confirmsNeeded, uint8 disputesNeeded, uint256 rewardPerConfirm);
    event RewardsFunded(address indexed from, uint256 amount, uint256 pool);
    event PayoutDeferred(address indexed to, uint256 amount);

    // ------------------------------------------------------------------ errors
    error NotOwner();
    error NotOperator();
    error AlreadyOperator();
    error BadInput();
    error NotYourRobot();
    error RobotTaken();
    error StakeTooLow();
    error UnknownHazard();
    error WrongStatus();
    error SameOperator();
    error AlreadyVoted();
    error Reentrancy();
    error NothingToWithdraw();
    error TransferFailed();

    // ------------------------------------------------------------------ modifiers
    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyOperator() {
        if (bytes(operatorName[msg.sender]).length == 0) revert NotOperator();
        _;
    }

    modifier nonReentrant() {
        if (_lock != 1) revert Reentrancy();
        _lock = 2;
        _;
        _lock = 1;
    }

    constructor(uint256 _minStake, uint8 _confirmsNeeded, uint8 _disputesNeeded, uint256 _rewardPerConfirm) {
        owner = msg.sender;
        _setConfig(_minStake, _confirmsNeeded, _disputesNeeded, _rewardPerConfirm);
    }

    // ------------------------------------------------------------------ admin
    function registerOperator(address signer, string calldata name) external onlyOwner {
        if (signer == address(0) || bytes(name).length == 0) revert BadInput();
        if (bytes(operatorName[signer]).length != 0) revert AlreadyOperator();
        operatorName[signer] = name;
        operatorList.push(signer);
        emit OperatorRegistered(signer, name);
    }

    function setConfig(uint256 _minStake, uint8 _confirmsNeeded, uint8 _disputesNeeded, uint256 _rewardPerConfirm)
        external
        onlyOwner
    {
        _setConfig(_minStake, _confirmsNeeded, _disputesNeeded, _rewardPerConfirm);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert BadInput();
        owner = newOwner;
    }

    function fundRewards() external payable {
        rewardPool += msg.value;
        emit RewardsFunded(msg.sender, msg.value, rewardPool);
    }

    function _setConfig(uint256 _minStake, uint8 _confirmsNeeded, uint8 _disputesNeeded, uint256 _rewardPerConfirm)
        internal
    {
        if (_confirmsNeeded == 0 || _disputesNeeded == 0) revert BadInput();
        minStake = _minStake;
        confirmsNeeded = _confirmsNeeded;
        disputesNeeded = _disputesNeeded;
        rewardPerConfirm = _rewardPerConfirm;
        emit ConfigUpdated(_minStake, _confirmsNeeded, _disputesNeeded, _rewardPerConfirm);
    }

    // ------------------------------------------------------------------ operators
    /// @notice Register robots for the calling operator. kinds: 0=rover, 1=drone. Re-registering your own
    ///         robot updates its kind; a robot owned by another operator reverts.
    function registerRobots(bytes32[] calldata robotIds, uint8[] calldata kinds) external onlyOperator {
        uint256 n = robotIds.length;
        if (n == 0 || n != kinds.length) revert BadInput();
        for (uint256 i; i < n; ++i) {
            if (kinds[i] > 1) revert BadInput();
            address cur = robots[robotIds[i]].operator;
            if (cur != address(0) && cur != msg.sender) revert RobotTaken();
            robots[robotIds[i]] = Robot(msg.sender, kinds[i]);
        }
        emit RobotsRegistered(msg.sender, robotIds, kinds);
    }

    function reportHazard(
        bytes32 robotId,
        bytes32 edgeId,
        string calldata edgeName,
        uint8 hazardType,
        uint8 severity,
        uint8 passableMask,
        bytes32 evidenceRoot,
        string calldata zgRequestId
    ) external payable onlyOperator returns (uint256 hazardId) {
        if (robots[robotId].operator != msg.sender) revert NotYourRobot();
        if (msg.value < minStake) revert StakeTooLow();
        if (hazardType == 0 || hazardType > 3 || severity > 100 || passableMask > 3) revert BadInput();
        if (msg.value > type(uint128).max) revert BadInput();
        _hazards.push(
            Hazard({
                reporter: msg.sender,
                hazardType: hazardType,
                severity: severity,
                passableMask: passableMask,
                status: Status.Reported,
                confirms: 0,
                disputes: 0,
                reportedAt: uint64(block.timestamp),
                stake: uint128(msg.value),
                robotId: robotId,
                edgeId: edgeId,
                evidenceRoot: evidenceRoot
            })
        );
        hazardId = _hazards.length;
        emit HazardReported(
            hazardId,
            edgeId,
            msg.sender,
            robotId,
            edgeName,
            hazardType,
            severity,
            passableMask,
            msg.value,
            evidenceRoot,
            zgRequestId
        );
    }

    function verifyHazard(uint256 hazardId, bytes32 robotId, bool confirm, bytes32 evidenceRoot, string calldata zgRequestId)
        external
        onlyOperator
        nonReentrant
    {
        Hazard storage h = _get(hazardId);
        if (h.status != Status.Reported) revert WrongStatus();
        if (robots[robotId].operator != msg.sender) revert NotYourRobot();
        if (h.reporter == msg.sender) revert SameOperator();
        if (hasVoted[hazardId][msg.sender]) revert AlreadyVoted();
        hasVoted[hazardId][msg.sender] = true;

        if (confirm) {
            h.confirms += 1;
        } else {
            h.disputes += 1;
            _disputers[hazardId].push(msg.sender);
        }
        emit HazardVerified(hazardId, msg.sender, robotId, confirm, h.confirms, h.disputes, evidenceRoot, zgRequestId);

        if (confirm && h.confirms >= confirmsNeeded) {
            // effects first
            h.status = Status.Confirmed;
            uint256 stake = h.stake;
            h.stake = 0;
            uint256 reward = rewardPerConfirm < rewardPool ? rewardPerConfirm : rewardPool;
            rewardPool -= reward;
            address reporter = h.reporter;
            emit HazardResolved(hazardId, uint8(Status.Confirmed), reporter, stake + reward);
            // interactions last
            _pay(reporter, stake + reward);
        } else if (!confirm && h.disputes >= disputesNeeded) {
            h.status = Status.Rejected;
            uint256 stake = h.stake;
            h.stake = 0;
            emit HazardResolved(hazardId, uint8(Status.Rejected), h.reporter, stake);
            address[] storage ds = _disputers[hazardId];
            uint256 n = ds.length;
            uint256 share = stake / n;
            for (uint256 i; i < n; ++i) {
                // last disputer gets the rounding dust
                _pay(ds[i], i == n - 1 ? stake - share * (n - 1) : share);
            }
        }
    }

    function clearHazard(uint256 hazardId, bytes32 evidenceRoot, string calldata zgRequestId) external onlyOperator {
        Hazard storage h = _get(hazardId);
        if (h.status != Status.Confirmed) revert WrongStatus();
        h.status = Status.Cleared;
        emit HazardCleared(hazardId, msg.sender, evidenceRoot, zgRequestId);
    }

    function recordDelivery(bytes32 orderId, bytes32 robotId, bytes32 episodeRoot) external onlyOperator {
        if (robots[robotId].operator != msg.sender) revert NotYourRobot();
        emit DeliveryRecorded(orderId, msg.sender, robotId, episodeRoot);
    }

    /// @notice Pull any payout whose push transfer failed (e.g. a contract operator that rejects ETH).
    function withdraw() external nonReentrant {
        uint256 amt = credits[msg.sender];
        if (amt == 0) revert NothingToWithdraw();
        credits[msg.sender] = 0;
        (bool ok,) = msg.sender.call{value: amt}("");
        if (!ok) revert TransferFailed();
    }

    // ------------------------------------------------------------------ views
    function hazardCount() external view returns (uint256) {
        return _hazards.length;
    }

    function getHazard(uint256 hazardId) external view returns (Hazard memory) {
        if (hazardId == 0 || hazardId > _hazards.length) revert UnknownHazard();
        return _hazards[hazardId - 1];
    }

    function getDisputers(uint256 hazardId) external view returns (address[] memory) {
        return _disputers[hazardId];
    }

    function operatorCount() external view returns (uint256) {
        return operatorList.length;
    }

    function isOperator(address a) external view returns (bool) {
        return bytes(operatorName[a]).length != 0;
    }

    // ------------------------------------------------------------------ internal
    function _get(uint256 hazardId) internal view returns (Hazard storage) {
        if (hazardId == 0 || hazardId > _hazards.length) revert UnknownHazard();
        return _hazards[hazardId - 1];
    }

    function _pay(address to, uint256 amt) internal {
        if (amt == 0) return;
        (bool ok,) = to.call{value: amt, gas: 30000}("");
        if (!ok) {
            credits[to] += amt;
            emit PayoutDeferred(to, amt);
        }
    }
}
