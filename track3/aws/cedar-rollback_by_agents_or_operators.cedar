permit(
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"track3-tools___rollback_model",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-2:279760855394:gateway/track3-gw-vtgfbwgbqu"
) when {
  principal.id == "arn:aws:sts::279760855394:assumed-role/team04-track3-runtime" ||
  principal.id like "arn:aws:sts::279760855394:assumed-role/AWSReservedSSO_bb26-Participant_*"
};
