from .. import ctrl_class
from dash import html, dcc
from dash.dependencies import Input, Output, State


def return_obj(dash_app, engine):
	ctrl_id = "07_trigger_record_select_ctrl"

	ctrl_div = html.Div([
		dcc.Dropdown(
			id=ctrl_id
		)
	],style={"marginBottom":"1.5em"})

	ctrl = ctrl_class.ctrl("trigger_record_select", ctrl_id, ctrl_div, engine)
	ctrl.add_ctrl("06_file_select_ctrl")

	init_callbacks(dash_app, engine)
	return(ctrl)

def init_callbacks(dash_app, engine):
	@dash_app.callback(
		Output('07_trigger_record_select_ctrl', 'options'),
		Input('file_storage_id', 'data')
		)
	def update_trigger_record_select(raw_data_file):
		if not raw_data_file:
			return []
		tr_nums = [{'label':str(n), 'value':str(n)} for n in engine.get_trigger_record_list(raw_data_file)]
		return(tr_nums)
