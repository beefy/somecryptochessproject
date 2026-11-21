// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title EthHtlc
/// @notice The Ethereum leg of an ETH<->ZQWX atomic swap.
/// @dev There is no owner, proxy, upgrade path or admin function. The contract
/// holds ETH only between a lock and its claim/refund, and the SCCP chain
/// independently holds the other leg under the same SHA-256 hashlock. A swap is
/// only as strong as its weaker leg: this side is secp256k1 and keccak, while
/// SCCP accounts are hybrid Ed25519 + ML-DSA-65.
contract EthHtlc {
    struct Lock {
        address depositor;
        address payable beneficiary;
        bytes32 hashlock;
        uint64 timelock;
        uint256 amount;
        bool claimed;
        bool refunded;
    }

    mapping(bytes32 => Lock) private _locks;
    mapping(address => uint256) private _nonces;

    event Locked(
        bytes32 indexed id,
        address indexed depositor,
        address indexed beneficiary,
        bytes32 hashlock,
        uint64 timelock,
        uint256 amount
    );
    event Claimed(bytes32 indexed id, address indexed beneficiary, bytes32 preimage);
    event Refunded(bytes32 indexed id, address indexed depositor);

    error EmptyValue();
    error EmptyHashlock();
    error ZeroBeneficiary();
    error TimelockInThePast();
    error LockExists();
    error UnknownLock();
    error AlreadyClosed();
    error NotYetClaimable();
    error Expired();
    error WrongPreimage();
    error OnlyDepositor();
    error TransferFailed();
    error NoEther();

    /// @notice Lock ETH under a SHA-256 hashlock and an absolute Unix timelock.
    /// @param hashlock sha256 of the 32-byte preimage both chains share.
    /// @param timelock Unix seconds after which the depositor may refund.
    /// @param beneficiary The address that may claim with the preimage.
    /// @return id The lock id; also emitted in the `Locked` event.
    function lock(bytes32 hashlock, uint64 timelock, address payable beneficiary)
        external
        payable
        returns (bytes32 id)
    {
        if (msg.value == 0) revert EmptyValue();
        if (hashlock == bytes32(0)) revert EmptyHashlock();
        if (beneficiary == address(0)) revert ZeroBeneficiary();
        if (timelock <= block.timestamp) revert TimelockInThePast();

        uint256 nonce = _nonces[msg.sender]++;
        id = keccak256(
            abi.encode(
                block.chainid,
                address(this),
                msg.sender,
                nonce,
                msg.value,
                hashlock,
                timelock,
                beneficiary
            )
        );
        if (_locks[id].depositor != address(0)) revert LockExists();

        _locks[id] = Lock({
            depositor: msg.sender,
            beneficiary: beneficiary,
            hashlock: hashlock,
            timelock: timelock,
            amount: msg.value,
            claimed: false,
            refunded: false
        });
        emit Locked(id, msg.sender, beneficiary, hashlock, timelock, msg.value);
    }

    /// @notice Claim a lock by revealing the preimage before its timelock.
    function claim(bytes32 id, bytes32 preimage) external {
        Lock storage lockData = _locks[id];
        if (lockData.depositor == address(0)) revert UnknownLock();
        if (lockData.claimed || lockData.refunded) revert AlreadyClosed();
        if (block.timestamp >= lockData.timelock) revert Expired();
        if (sha256(abi.encodePacked(preimage)) != lockData.hashlock) revert WrongPreimage();

        lockData.claimed = true;
        address payable beneficiary = lockData.beneficiary;
        uint256 amount = lockData.amount;
        (bool ok,) = beneficiary.call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit Claimed(id, beneficiary, preimage);
    }

    /// @notice Refund a lock to its depositor at or after its timelock.
    function refund(bytes32 id) external {
        Lock storage lockData = _locks[id];
        if (lockData.depositor == address(0)) revert UnknownLock();
        if (lockData.claimed || lockData.refunded) revert AlreadyClosed();
        if (msg.sender != lockData.depositor) revert OnlyDepositor();
        if (block.timestamp < lockData.timelock) revert NotYetClaimable();

        lockData.refunded = true;
        address payable depositor = payable(lockData.depositor);
        uint256 amount = lockData.amount;
        (bool ok,) = depositor.call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit Refunded(id, depositor);
    }

    /// @notice Read one lock. A zero depositor means the id is unknown.
    function getLock(bytes32 id) external view returns (Lock memory) {
        return _locks[id];
    }

    /// @dev Sweeping stray ETH into a lock would make it unattributable.
    receive() external payable {
        revert NoEther();
    }
}
