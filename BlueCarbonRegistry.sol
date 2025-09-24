// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract BlueCarbonRegistry {

    struct Batch {
        string batchHash;
        string areaId;
        uint256 timestamp;
        address uploader;
    }

    Batch[] public batches;

    event BatchCommitted(uint256 indexed batchIndex, string batchHash, string areaId, uint256 timestamp, address uploader);

    function commitHash(string memory batchHash, string memory areaId, uint256 timestamp) public {
        batches.push(Batch(batchHash, areaId, timestamp, msg.sender));
        emit BatchCommitted(batches.length - 1, batchHash, areaId, timestamp, msg.sender);
    }

    function getBatch(uint256 index) public view returns (string memory, string memory, uint256, address) {
        require(index < batches.length, "Invalid index");
        Batch memory b = batches[index];
        return (b.batchHash, b.areaId, b.timestamp, b.uploader);
    }

    function totalBatches() public view returns (uint256) {
        return batches.length;
    }
}
