local Signal = {}

function Signal.new()
	return { connections = {} }
end

return Signal
