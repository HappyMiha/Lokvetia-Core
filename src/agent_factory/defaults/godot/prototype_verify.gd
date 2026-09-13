extends Node
## Opt-in acceptance driver in the exported game, using real input and physics.
var game: Node2D
var player: CharacterBody2D
var checks: Dictionary = {}
var measurements: Dictionary = {}
var output := ""
var expected_jumps := 0

func _ready() -> void:
    var args := OS.get_cmdline_user_args()
    if not "--verify-cycle" in args:
        return
    var index := args.find("--evidence-dir")
    var expected := args.find("--expected-jumps")
    if index < 0 or index + 1 >= args.size() or expected < 0 or expected + 1 >= args.size():
        get_tree().quit(2)
        return
    output = args[index + 1]
    expected_jumps = int(args[expected + 1])
    game = get_parent()
    player = game.get_node("Player")
    run.call_deferred()

func frames(count: int) -> void:
    for index in count:
        await get_tree().physics_frame
        await get_tree().process_frame

func press(action: String, count: int = 1) -> void:
    Input.action_press(action)
    await frames(count)
    Input.action_release(action)
    await frames(1)

func landed() -> bool:
    for index in 180:
        await frames(1)
        if player.is_on_floor():
            return true
    return false

func move_to(target: float) -> void:
    Input.action_press("ui_right")
    for index in 180:
        await frames(1)
        if player.position.x >= target or game._finished:
            break
    Input.action_release("ui_right")
    await frames(1)

func run() -> void:
    await frames(60)
    checks["floor_collision"] = player.is_on_floor()
    var original_x := player.position.x
    await press("ui_right", 12)
    checks["horizontal_input"] = player.position.x > original_x + 20.0
    await press("ui_left", 12)
    checks["left_input"] = abs(player.position.x - original_x) < 10.0
    var start_y := player.position.y
    await press("ui_accept")
    await frames(10)
    checks["ground_jump"] = player.position.y < start_y - 35.0 and not player.is_on_floor()
    var before := player.velocity.y
    await press("ui_accept")
    var after := player.velocity.y
    measurements["air_jump_velocity_before"] = before
    measurements["air_jump_velocity_after"] = after
    var air_jump := after < before - 70.0
    checks["requested_air_jump_behavior"] = air_jump == (expected_jumps == 2)
    await frames(8)
    before = player.velocity.y
    await press("ui_accept")
    checks["no_extra_jump"] = player.velocity.y > before
    checks["lands_again"] = await landed()
    await press("ui_accept")
    checks["jump_rearmed_on_landing"] = player.velocity.y < -400.0
    # Reposition only for collision/restart boundary checks, never jump checks.
    player.position = Vector2(930.0, 200.0)
    player.velocity = Vector2.ZERO
    await frames(4)
    checks["goal_collision_wins"] = game._finished and "reached the goal" in game.get_node("HUD/Status").text
    await press("ui_accept")
    checks["restart_after_win"] = not game._finished and player.position.x < 150.0
    player.position = Vector2(1100.0, 950.0)
    await frames(4)
    checks["fall_loses"] = game._finished and "fell off" in game.get_node("HUD/Status").text
    await press("ui_accept")
    checks["restart_after_loss"] = not game._finished and player.position.x < 150.0
    await landed()
    if DisplayServer.get_name() != "headless":
        await RenderingServer.frame_post_draw
        var picture := get_viewport().get_texture().get_image()
        checks["rendered_frame"] = picture.get_width() == 1152 and picture.get_height() == 648 and picture.save_png(output.path_join("game.png")) == OK
    # Complete the actual level from spawn using input only, without teleporting.
    await move_to(180.0)
    await press("ui_accept")
    # Clear the first solid platform's underside before moving over its edge.
    await frames(24)
    await move_to(300.0)
    await landed()
    measurements["first_platform_position"] = [player.position.x, player.position.y]
    await move_to(400.0)
    await press("ui_accept")
    await move_to(620.0)
    await landed()
    measurements["second_platform_position"] = [player.position.x, player.position.y]
    await move_to(700.0)
    await press("ui_accept")
    await move_to(930.0)
    await frames(30)
    measurements["goal_position"] = [player.position.x, player.position.y]
    checks["level_completed_with_input"] = game._finished and "reached the goal" in game.get_node("HUD/Status").text
    var passed := true
    for value in checks.values():
        passed = passed and bool(value)
    var evidence := {"passed": passed, "checks": checks, "measurements": measurements, "expected_jumps": expected_jumps, "display": DisplayServer.get_name()}
    var file := FileAccess.open(output.path_join("runtime.json"), FileAccess.WRITE)
    if file == null:
        get_tree().quit(3)
        return
    file.store_string(JSON.stringify(evidence, "  "))
    file.close()
    get_tree().quit(0 if passed else 1)
