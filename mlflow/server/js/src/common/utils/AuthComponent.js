import React, { Component } from 'react';
import { connect } from 'react-redux';
import Utils from '../../common/utils/Utils';
import { Redirect } from 'react-router';
import PropTypes from 'prop-types';

export class AuthComponentImpl extends Component {
  constructor(props) {
    super(props);
    const urlState = Utils.getSearchParamsFromUrl(props.location.search);
    this.state = {
      persistedState: {
        code: urlState.code === undefined ? '' : urlState.code,
      },
    };
  }

  static propTypes = {
    location: PropTypes.object,
  };

  componentDidMount = () => {
    const req = new XMLHttpRequest();
    req.open('GET', '/token?code=' + this.state.persistedState.code, false);
    req.send();
    if (req.status === 200) {
      const token = req.getResponseHeader(req.getResponseHeader('Access-Control-Expose-Headers'));
      localStorage.setItem('token', token);
    }
  };

  render() {
    return <Redirect to='/'/>;
  };
}

export const AuthComponent = connect(null, null)(AuthComponentImpl);
