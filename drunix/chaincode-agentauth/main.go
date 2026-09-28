/*
SPDX-License-Identifier: Apache-2.0

agentauth chaincode entry point.

Two launch modes, both standard for Drunix / Fabric 2.5:

  - Peer-launched (default): the Drunix peer builds and starts this binary in a
    chaincode container (./network.sh deployCC ...).
  - Chaincode-as-a-service: when CHAINCODE_SERVER_ADDRESS and CHAINCODE_ID are
    set, the binary runs as a gRPC server and the peer connects to it through
    the ccaas external builder (used by the native runtime in drunix/scripts).
*/

package main

import (
	"log"
	"os"
	"strings"

	"github.com/hyperledger/fabric-chaincode-go/v2/shim"
	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"

	"agentauth/authority"
)

func main() {
	contract := &authority.Contract{}
	contract.Name = "agentauth"
	cc, err := contractapi.NewChaincode(contract)
	if err != nil {
		log.Panicf("error creating agentauth chaincode: %v", err)
	}
	cc.Info.Title = "AgentGuard financial-authority ledger"
	cc.Info.Version = authority.ContractVersion
	cc.DefaultContract = "agentauth"

	address := os.Getenv("CHAINCODE_SERVER_ADDRESS")
	ccid := os.Getenv("CHAINCODE_ID")
	if address == "" || ccid == "" {
		if err := cc.Start(); err != nil {
			log.Panicf("error starting agentauth chaincode: %v", err)
		}
		return
	}

	tls := shim.TLSProperties{Disabled: true}
	if strings.EqualFold(os.Getenv("CHAINCODE_TLS_DISABLED"), "false") {
		key, err := os.ReadFile(os.Getenv("CHAINCODE_TLS_KEY"))
		if err != nil {
			log.Panicf("cannot read CHAINCODE_TLS_KEY: %v", err)
		}
		cert, err := os.ReadFile(os.Getenv("CHAINCODE_TLS_CERT"))
		if err != nil {
			log.Panicf("cannot read CHAINCODE_TLS_CERT: %v", err)
		}
		tls = shim.TLSProperties{Disabled: false, Key: key, Cert: cert}
	}
	server := &shim.ChaincodeServer{CCID: ccid, Address: address, CC: cc, TLSProps: tls}
	log.Printf("agentauth chaincode-as-a-service listening on %s", address)
	if err := server.Start(); err != nil {
		log.Panicf("error starting agentauth chaincode server: %v", err)
	}
}
